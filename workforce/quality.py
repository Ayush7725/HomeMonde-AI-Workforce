"""AI quality control (Lock 16).

1. Reviews: whether a finding or recommendation turned out right, from a person or from the rule's automatic
   outcome check after N days. Accuracy per employee = (correct + half of partly) / reviewed; a person's verdict
   overrides the automatic one.
2. Scenario suite: tenants/<id>/evals.yaml lists known business situations with the expected work, findings and
   recommendations. Each scenario runs the real pipeline (rules, tools, models, validation) in a throwaway database;
   results are stored per model pair so providers can be compared on identical scenarios."""
import hashlib
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import ai_settings, data, db, events, expr, notify, providers, rules, runtime, tenancy, work
from .db import utcnow
from .settings import get_settings
from .metrics import MetricEngine
from .models import ActionRecord, Evidence, EvalResult, FindingReview, LlmUsage, Record, SourceState, Tenant, User, WorkItem
from .tenancy import ModelRoute, TenantConfig

VERDICTS = ("CORRECT", "PARTLY", "WRONG")


class ReviewError(Exception):
    pass


# ---------------------------------------------------------------- reviews and accuracy

def review(db_: Session, cfg: TenantConfig, work_id: str, user: User, verdict: str, note: str = "") -> FindingReview:
    if verdict not in VERDICTS:
        raise ReviewError("verdict must be CORRECT, PARTLY or WRONG")
    item = db_.get(WorkItem, (cfg.id, work_id))
    if item is None or item.parent_id or not item.finding:
        raise ReviewError("only findings can be reviewed")
    row = db_.execute(select(FindingReview).where(FindingReview.tenant_id == cfg.id, FindingReview.work_id == work_id,
                                                  FindingReview.source == "human")).scalar_one_or_none()
    if row is None:
        row = FindingReview(tenant_id=cfg.id, work_id=work_id, employee=item.employee, source="human")
        db_.add(row)
    row.verdict, row.note, row.reviewer, row.created_at = verdict, note.strip()[:1000], user.id, utcnow()
    events.emit(db_, cfg.id, "FINDING_REVIEWED", f"{user.name} marked {work_id} {verdict.lower()}" + (f" · {row.note}" if row.note else ""),
                actor=f"user:{user.id}", work_id=work_id, emp=item.employee, verdict=verdict)
    if verdict == "WRONG":
        _ask_for_test(db_, cfg, item)
    return row


def _ask_for_test(db_: Session, cfg: TenantConfig, item: WorkItem) -> None:
    """A wrong answer becomes a quality test, so the same mistake is caught before it can happen again."""
    if scenario_id(item.id) in {sc.get("id") for sc in load_suite(cfg.id)}:
        return
    notify.send(db_, cfg, kind="quality", severity="MEDIUM", title=f"Turn {item.id} into a quality test",
                verified=f"{item.id} was marked wrong. Download its test draft from the task, write down the right answer, "
                         f"and add it to the quality tests so this mistake is checked on every run.",
                work_id=item.id, employee=item.employee)


def override_rate(db_: Session, tenant_id: str, days: int = 30) -> dict[str, dict]:
    """How often people did not accept an AI employee's recommendation as proposed (changed, rejected or sent back)."""
    from .models import Approval
    since = utcnow() - timedelta(days=days)
    out: dict[str, dict] = {}
    for a in db_.execute(select(Approval).where(Approval.tenant_id == tenant_id, Approval.decided_at >= since,
                                                Approval.status.in_(["APPROVED", "MODIFIED", "REJECTED", "MORE_INVESTIGATION"]))).scalars():
        e = out.setdefault(a.employee, {"decided": 0, "accepted": 0, "changed": 0, "rejected": 0, "sent_back": 0})
        e["decided"] += 1
        e[{"APPROVED": "accepted", "MODIFIED": "changed", "REJECTED": "rejected", "MORE_INVESTIGATION": "sent_back"}[a.status]] += 1
    for e in out.values():
        e["rate"] = round((e["decided"] - e["accepted"]) / e["decided"], 3)
    return out


RULE_REVIEW_MIN = 5  # judged alerts needed before a rule's track record means anything
RULE_REVIEW_SHARE = 0.5  # more than half judged wrong or turned down: the rule itself needs a look


def rule_feedback(db_: Session, cfg: TenantConfig, days: int = 30) -> dict[str, dict]:
    """Per rule: how its alerts were judged by people in the last `days` (a review verdict, else the decision on its
    suggestion). Turned down = marked wrong, or the suggestion rejected."""
    from .models import Approval
    since = utcnow() - timedelta(days=days)
    items = db_.execute(select(WorkItem.id, WorkItem.rule_id).where(
        WorkItem.tenant_id == cfg.id, WorkItem.parent_id.is_(None), WorkItem.rule_id.isnot(None), WorkItem.created_at >= since)).all()
    rule_of = dict(items)
    verdict: dict[str, str] = {}
    for r in db_.execute(select(FindingReview).where(FindingReview.tenant_id == cfg.id, FindingReview.work_id.in_(list(rule_of)))).scalars():
        if r.work_id not in verdict or r.source == "human":
            verdict[r.work_id] = "down" if r.verdict == "WRONG" else "up"
    for a in db_.execute(select(Approval).where(Approval.tenant_id == cfg.id, Approval.work_id.in_(list(rule_of)),
                                                Approval.status.in_(["APPROVED", "MODIFIED", "REJECTED"]))).scalars():
        verdict.setdefault(a.work_id, "down" if a.status == "REJECTED" else "up")
    out: dict[str, dict] = {}
    for wid, rid in rule_of.items():
        e = out.setdefault(rid, {"alerts": 0, "judged": 0, "turned_down": 0})
        e["alerts"] += 1
        if wid in verdict:
            e["judged"] += 1
            e["turned_down"] += verdict[wid] == "down"
    for e in out.values():
        e["share_turned_down"] = round(e["turned_down"] / e["judged"], 3) if e["judged"] else None
    return out


def propose_rule_reviews(db_: Session, cfg: TenantConfig) -> int:
    """Learning from corrections: a rule whose alerts people keep turning down gets a review task for the owners,
    with its track record and condition. Rules are never changed automatically; an owner edits the company pack."""
    made = 0
    for rule in cfg.rules:
        fb = rule_feedback(db_, cfg).get(rule.id)
        if not fb or fb["judged"] < RULE_REVIEW_MIN or fb["share_turned_down"] <= RULE_REVIEW_SHARE:
            continue
        key = f"rule_review:{rule.id}"
        if work.open_or_recent(db_, cfg.id, key, 30 * 24):
            continue
        cond = rule.when or rule.kind.replace("_", " ")
        facts = [f"In the last 30 days people judged {fb['judged']} of its {fb['alerts']} alerts and turned down {fb['turned_down']}.",
                 f"It raises an alert when: {cond}.",
                 "Rules are never changed automatically. If the threshold no longer fits, an owner updates it in the company settings."]
        emp = cfg.question_employee()
        item = work.create(db_, cfg, employee=emp, type_="RULE_REVIEW", priority="MEDIUM", status="NEEDS_HUMAN_REVIEW",
                           title=f"Review the rule: {rule.description or rule.id}"[:300], subject=rule.id, origin="quality",
                           dedupe_key=key, context={"rule_id": rule.id, "feedback": fb},
                           next_step="An owner decides whether the rule should change", msg=f"Rule {rule.id} is turned down too often; review proposed")
        item.finding = {"outcome": "FINDING", "headline": f"People turned down {fb['turned_down']} of {fb['judged']} recent alerts from this rule",
                        "statements": [{"kind": "FACT", "text": f, "refs": []} for f in facts], "evidence_quality": "CONFIRMED",
                        "quality_basis": ["Counted from people's own reviews and decisions"], "impact": "", "unknowns": [],
                        "recommendation": None, "ai": False}
        made += 1
    return made


def accuracy(db_: Session, tenant_id: str, days: int = 90) -> dict[str, dict]:
    since = utcnow() - timedelta(days=days)
    best: dict[str, FindingReview] = {}
    for r in db_.execute(select(FindingReview).where(FindingReview.tenant_id == tenant_id, FindingReview.created_at >= since)).scalars():
        if r.work_id not in best or r.source == "human":
            best[r.work_id] = r
    out: dict[str, dict] = {}
    for r in best.values():
        e = out.setdefault(r.employee, {"correct": 0, "partly": 0, "wrong": 0})
        e[r.verdict.lower()] += 1
    for e in out.values():
        n = e["correct"] + e["partly"] + e["wrong"]
        e["reviewed"] = n
        e["score"] = round((e["correct"] + 0.5 * e["partly"]) / n, 3) if n else None
    return out


def check_outcomes(db_: Session, cfg: TenantConfig) -> int:
    """Run each rule's outcome check once it is due. Skips while the data needed is not current."""
    now, done = utcnow(), 0
    for rule in cfg.rules:
        oc = rule.outcome_check
        if not oc:
            continue
        reviewed = select(FindingReview.work_id).where(FindingReview.tenant_id == cfg.id, FindingReview.source == "auto")
        due = db_.execute(select(WorkItem).where(
            WorkItem.tenant_id == cfg.id, WorkItem.rule_id == rule.id, WorkItem.status == "COMPLETED", WorkItem.parent_id.is_(None),
            WorkItem.created_at <= now - timedelta(days=oc.after_days), WorkItem.id.notin_(reviewed))).scalars().all()
        if not due:
            continue
        if any(data.freshness(db_, cfg, d)[0] != "CURRENT" for d in rules.rule_datasets(cfg, rule)):
            continue
        engine = MetricEngine(db_, cfg)
        for item in due:
            taken = 1 if db_.execute(select(ActionRecord.id).where(
                ActionRecord.tenant_id == cfg.id, ActionRecord.work_id == item.id, ActionRecord.status.in_(["EXECUTED", "VERIFIED"]))).first() else 0
            results = []
            for group in work.item_groups(item.context) or [None]:  # a batched alert is right only if right for every group
                env = {m: engine.for_group(m, rule.for_each, group) for m in expr.names(oc.correct_when) if m != "action_taken"}
                env["action_taken"] = taken
                results.append(expr.evaluate(oc.correct_when, env))
            if any(r is None for r in results):
                continue
            ok = all(results)
            verdict = "CORRECT" if ok else "WRONG"
            db_.add(FindingReview(tenant_id=cfg.id, work_id=item.id, employee=item.employee, source="auto", verdict=verdict,
                                  note=f"{oc.correct_when} checked after {oc.after_days} days"))
            events.emit(db_, cfg.id, "OUTCOME_CHECKED", f"Outcome check for {item.id}: {verdict.lower()} ({oc.correct_when})",
                        work_id=item.id, emp=item.employee, verdict=verdict)
            if verdict == "WRONG":
                _ask_for_test(db_, cfg, item)
            done += 1
    return done


# ---------------------------------------------------------------- scenario suite

def suite_path(slug: str) -> Path:
    return tenancy.pack_path(slug).parent / "evals.yaml"


def load_suite(slug: str) -> list[dict]:
    p = suite_path(slug)
    if not p.exists():
        return []
    scenarios = yaml.safe_load(p.read_text()) or []
    if not isinstance(scenarios, list):
        raise ValueError(f"{p}: expected a list of scenarios")
    return scenarios


def scenario_id(work_id: str) -> str:
    return "case_" + work_id.lower().replace("-", "_")


def needs_test(db_: Session, cfg: TenantConfig) -> list[dict]:
    """Findings judged wrong (a person's verdict beats the automatic one) that are not in the quality tests yet."""
    best: dict[str, FindingReview] = {}
    for r in db_.execute(select(FindingReview).where(FindingReview.tenant_id == cfg.id)).scalars():
        if r.work_id not in best or r.source == "human":
            best[r.work_id] = r
    have = {sc.get("id") for sc in load_suite(cfg.id)}
    return [{"work_id": r.work_id, "employee": r.employee, "note": r.note, "source": r.source}
            for r in best.values() if r.verdict == "WRONG" and scenario_id(r.work_id) not in have]


def _anon(value) -> str:
    return "anon-" + hashlib.sha256(str(value).encode()).hexdigest()[:8]


def draft_scenario(db_: Session, cfg: TenantConfig, work_id: str) -> str:
    """A quality-test draft rebuilt from the exact source records a finding relied on, dated relative to the day it ran.
    The right answer is left for a person to write: the draft fails until they do."""
    item = db_.get(WorkItem, (cfg.id, work_id))
    if item is None or item.parent_id or not item.finding:
        raise ReviewError("only findings can be turned into tests")
    ev = db_.execute(select(Evidence).where(Evidence.tenant_id == cfg.id, Evidence.work_id == work_id)).scalars().all()
    ids = sorted({i for e in ev for i in (e.record_ids or [])})
    truncated = [e.name for e in ev if (e.record_count or 0) > len(e.record_ids or [])]
    ref = item.created_at.date()
    datasets: dict[str, list] = {}
    latest: dict[tuple, Record] = {}
    for rec in db_.execute(select(Record).where(Record.tenant_id == cfg.id, Record.id.in_(ids))).scalars() if ids else []:
        ds = cfg.datasets.get(rec.dataset)
        if ds is None:
            continue
        if ds.mode == "snapshot":  # a test holds one snapshot: the latest the finding saw for each key
            k = (rec.dataset, "|".join(str(rec.data.get(f)) for f in ds.key))
            if k not in latest or (rec.occurred_on or ref) > (latest[k].occurred_on or ref):
                latest[k] = rec
            continue
        datasets.setdefault(rec.dataset, []).append(_draft_row(ds, rec.data, ref))
    for (name, _), rec in latest.items():
        datasets.setdefault(name, []).append(_draft_row(cfg.datasets[name], rec.data, ref))
    expect: dict = {}
    if item.origin in ("rule", "check"):
        expect["work"] = [{"type": item.type, "group": g} for g in work.item_groups(item.context)]
    expect["findings"] = {item.type: {"outcome": ["WRITE_THE_RIGHT_ANSWER"]}}
    review_ = db_.execute(select(FindingReview).where(FindingReview.tenant_id == cfg.id, FindingReview.work_id == work_id)
                          .order_by(FindingReview.source)).scalars().first()
    sc = {"id": scenario_id(work_id), "description": f"{item.title} — marked wrong" + (f": {review_.note}" if review_ and review_.note else ""),
          "datasets": datasets, "expect": expect}
    if item.origin == "question":
        sc["question"] = item.context.get("question") or item.subject
    head = [f"# Quality-test draft from {work_id} ({item.created_at:%Y-%m-%d}). Dates are relative to the day it ran.",
            "# Before adding it to evals.yaml: replace WRITE_THE_RIGHT_ANSWER with the outcome that was right",
            "# (FINDING, NO_ISSUE, DATA_CONFLICT, DATA_STALE, ...) and add any quality/recommendation/mentions checks.",
            "# Personal fields are replaced with anon-... placeholders."]
    if truncated:
        head.append(f"# Some evidence used more rows than were kept for drill-down ({', '.join(sorted(set(truncated)))}); check totals.")
    return "\n".join(head) + "\n" + yaml.safe_dump([sc], sort_keys=False, allow_unicode=True, width=140)


def _draft_row(ds, row: dict, ref: date) -> dict:
    out = {}
    for name, spec in ds.fields.items():
        v = row.get(name)
        if v is None:
            continue
        if spec.sensitive:
            v = _anon(v)
        elif spec.type == "date":
            d = (ref - date.fromisoformat(str(v)[:10])).days
            if name == ds.date_field:
                out["days_ago"] = d
                continue
            v = "today" if d == 0 else (f"today-{d}" if d > 0 else f"today+{-d}")
        out[name] = v
    return out


def _test_runs(cfg: TenantConfig, sc: dict) -> int:
    """Higher-risk employees (those that can propose actions) are tested more than once: every run must pass."""
    if sc.get("runs"):
        return int(sc["runs"])
    types = {w["type"] for w in (sc.get("expect") or {}).get("work") or []}
    emps = {r.employee for r in cfg.rules if r.work_type in types}
    if sc.get("question"):
        emps.add(cfg.question_employee())
    n = 1
    for e in emps:
        ec = cfg.employees[e]
        n = max(n, ec.test_runs or (3 if ec.actions else 1))
    return n


def _date(value, today: date) -> str | None:
    """'today', 'today-4', 'today+2' or an ISO date."""
    if value is None or value == "":
        return None
    s = str(value).strip()
    if s.startswith("today"):
        rest = s[5:]
        return (today + timedelta(days=int(rest or 0))).isoformat()
    return date.fromisoformat(s).isoformat()


def _expand(ds, rows: list[dict], today: date) -> list[dict]:
    out = []
    for row in rows:
        ago = row.get("days_ago")
        if ago is None or not ds.date_field:
            spans = [None]
        else:
            lo, hi = (ago, ago) if isinstance(ago, int) else (ago[0], ago[1])
            spans = list(range(lo, hi + 1))
        for d in spans:
            rec = {}
            for name, spec in ds.fields.items():
                v = row.get(name)
                if name == ds.date_field and d is not None:
                    v = (today - timedelta(days=d)).isoformat()
                elif spec.type == "date":
                    v = _date(v, today)
                else:
                    v = data._convert(spec.model_copy(update={"format": None}), v)
                rec[name] = v
            if len(spans) > 1:  # one row per day: keep keys unique
                rec.update({k: f"{rec[k]}-{d}" for k in ds.key if k != ds.date_field and isinstance(rec[k], str)})
            out.append(rec)
    return out


def _load(db_: Session, cfg: TenantConfig, datasets: dict) -> None:
    today = data.today(cfg)
    now = utcnow()
    for name, rows in (datasets or {}).items():
        ds = cfg.datasets[name]
        for rec in _expand(ds, rows, today):
            natural = "|".join(str(rec[k]) for k in ds.key)
            key, occurred = (f"{natural}|{today}", today) if ds.mode == "snapshot" else \
                (natural, date.fromisoformat(rec[ds.date_field]) if ds.date_field and rec.get(ds.date_field) else None)
            db_.merge(Record(tenant_id=cfg.id, dataset=name, key=key, occurred_on=occurred, data=rec, synced_at=now))
    for sid in cfg.sources:
        db_.add(SourceState(tenant_id=cfg.id, source_id=sid, status="OK", last_attempt=now, last_success=now, error="",
                            rows=0, rejected=0, consecutive_failures=0))


def _run_all(cfg: TenantConfig) -> None:
    for _ in range(50):
        with db.session() as s:
            item = work.claim(s, [cfg.id], 600)
            wid = item.id if item else None
        if not wid:
            return
        with db.session() as s:
            runtime.run(s, cfg, s.get(WorkItem, (cfg.id, wid)))


def _check(name: str, ok, detail: str = "") -> dict:
    return {"check": name, "result": "skipped" if ok is None else ("pass" if ok else "fail"), "detail": detail}


def _finding_checks(exp: dict, item: WorkItem | None, tools_used: set[str]) -> list[dict]:
    if item is None:
        return [_check("finding produced", False, "no work item")]
    f = item.finding or {}
    ai = f.get("ai", True)
    out = []
    if "outcome" in exp:
        out.append(_check("outcome", f.get("outcome") in exp["outcome"] if ai else None,
                          f"got {f.get('outcome')}, expected one of {exp['outcome']}" if ai else "AI did not run"))
    if "quality" in exp:
        out.append(_check("evidence quality", f.get("evidence_quality") in exp["quality"] if ai else None,
                          f"got {f.get('evidence_quality')}" if ai else "AI did not run"))
    if "cites_tools" in exp:
        missing = set(exp["cites_tools"]) - tools_used
        out.append(_check("investigated with tools", not missing if ai else None, f"missing {sorted(missing)}" if missing else ""))
    if "recommendation" in exp:
        got = (f.get("recommendation") or {}).get("action", "none")
        out.append(_check("recommendation", got == exp["recommendation"] if ai else None, f"got {got}"))
    if exp.get("no_validation_notes"):
        notes = f.get("validation_notes") or []
        out.append(_check("no ungrounded claims", not notes if ai else None, "; ".join(notes)[:300]))
    if exp.get("mentions"):
        text = " ".join([f.get("headline", "")] + [s["text"] for s in f.get("statements", [])]).lower()
        miss = [m for m in exp["mentions"] if m.lower() not in text]
        out.append(_check("mentions", not miss if ai else None, f"missing {miss}" if miss else ""))
    return out


def _run_scenario(cfg: TenantConfig, sc: dict) -> tuple[list[dict], str, tuple]:
    """One run of one scenario in its own throwaway database: (checks, employee, (input, output, cost))."""
    with tempfile.TemporaryDirectory() as tmp:
        db.configure(f"sqlite:///{Path(tmp) / 'eval.db'}")
        try:
            db.Base.metadata.create_all(db.engine())
            with db.session() as s:
                s.add(Tenant(id=cfg.id, name=cfg.name))
                s.flush()
                _load(s, cfg, sc.get("datasets"))
            with db.session() as s:
                rules.evaluate(s, cfg)
            if sc.get("question"):
                with db.session() as s:
                    work.create(s, cfg, employee=cfg.question_employee(), type_="MANAGEMENT_QUESTION", title=sc["question"][:120],
                                subject=sc["question"], origin="question", context={"question": sc["question"]})
            _run_all(cfg)
            with db.session() as s:
                items = s.execute(select(WorkItem).where(WorkItem.parent_id.is_(None))).scalars().all()
                tools_used = {wid: set(s.execute(select(Evidence.name).where(Evidence.work_id == wid, Evidence.kind == "tool")).scalars())
                              for wid in [i.id for i in items]}
                usage = s.execute(select(func.coalesce(func.sum(LlmUsage.input_tokens + LlmUsage.cache_read_tokens), 0),
                                         func.coalesce(func.sum(LlmUsage.output_tokens), 0), func.sum(LlmUsage.cost_usd))).one()
                exp = sc.get("expect", {})
                checks = []
                rule_items = [i for i in items if i.origin in ("rule", "check")]
                if "work" in exp:
                    want = {(w["type"], str(w["group"])) for w in exp["work"]}
                    got = {(i.type, str(g)) for i in rule_items for g in work.item_groups(i.context)}
                    checks.append(_check("rules created the expected work", got == want, f"got {sorted(got)}"))
                for wtype, fexp in (exp.get("findings") or {}).items():
                    it = next((i for i in items if i.type == wtype), None)
                    checks += [{**c, "check": f"{wtype}: {c['check']}"} for c in _finding_checks(fexp, it, tools_used.get(it.id, set()) if it else set())]
                employee = next((i.employee for i in rule_items), None) or (cfg.question_employee() if sc.get("question") else "unknown")
                return checks, employee, (int(usage[0]), int(usage[1]), usage[2])
        finally:
            db.engine().dispose()


def run_suite(slug: str, strong: ModelRoute | None = None, cheap: ModelRoute | None = None,
              only: list[str] | None = None) -> tuple[str, list[EvalResult]]:
    """Run the scenario suite for a tenant. Real model calls are made (and billed) when credentials are set."""
    base = tenancy.get(slug)
    with db.session() as s:  # the owners' current model choices and keys, carried into the throwaway databases
        base = ai_settings.effective(s, base)
        snapshot = ai_settings.capture(s, base)
    for r in (strong, cheap):
        if r and r.provider not in providers.BUILTIN and r.provider not in base.ai.providers:
            raise ValueError(f"unknown provider {r.provider}")
    cfg = base.model_copy(update={"ai": base.ai.model_copy(update={k: v for k, v in (("strong", strong), ("cheap", cheap)) if v})})
    for tier, chosen in (("strong", strong), ("cheap", cheap)):
        if chosen:  # a model named for this run wins over the panel choice
            snapshot[0].pop(tier, None)
    run_id = f"EVAL-{utcnow():%Y%m%d%H%M}-{uuid.uuid4().hex[:4]}"
    label = lambda r: f"{r.provider}:{r.model}"  # noqa: E731
    main_url = get_settings().database_url
    results = []
    with ai_settings.preset(cfg.id, snapshot):
        _run_scenarios(cfg, slug, only, run_id, label, main_url, results)
    with db.session() as s:
        for r in results:
            s.add(r)
        if results:
            events.emit(s, cfg.id, "EVAL_RUN", f"AI-quality run {run_id}: {sum(r.status == 'PASSED' for r in results)} of {len(results)} "
                        f"scenarios passed on {label(cfg.ai.strong)} / {label(cfg.ai.cheap)}", run_id=run_id)
    return run_id, results


def _run_scenarios(cfg, slug, only, run_id, label, main_url, results) -> None:
    try:
        for sc in load_suite(slug):
            if only and sc["id"] not in only:
                continue
            outcomes = []
            for _ in range(_test_runs(cfg, sc)):
                outcomes.append(_run_scenario(cfg, sc))
                if any(c["result"] == "skipped" for c in outcomes[-1][0]):
                    break  # the AI did not run: repeating proves nothing
            runs = len(outcomes)
            checks = [c if runs == 1 else {**c, "check": f"run {i} of {runs}: {c['check']}"}
                      for i, (cs, _, _) in enumerate(outcomes, 1) for c in cs]
            employee = outcomes[0][1]
            usage = [sum(u[0] for _, _, u in outcomes), sum(u[1] for _, _, u in outcomes),
                     None if any(u[2] is None for _, _, u in outcomes) else sum(u[2] for _, _, u in outcomes)]
            status = "FAILED" if any(c["result"] == "fail" for c in checks) else ("SKIPPED" if any(c["result"] == "skipped" for c in checks) else "PASSED")
            results.append(EvalResult(tenant_id=cfg.id, run_id=run_id, scenario=sc["id"], employee=employee, status=status, checks=checks,
                                      strong_model=label(cfg.ai.strong), cheap_model=label(cfg.ai.cheap),
                                      input_tokens=int(usage[0]), output_tokens=int(usage[1]),
                                      cost_usd=round(usage[2], 6) if usage[2] is not None else None))
    finally:
        db.engine().dispose()
        db.configure(main_url)


def comparison(db_: Session, tenant_id: str) -> list[dict]:
    """Pass rate and cost per model pair across all runs: compare providers on the same scenarios."""
    rows = db_.execute(select(EvalResult).where(EvalResult.tenant_id == tenant_id).order_by(EvalResult.created_at.desc()).limit(2000)).scalars()
    agg: dict[tuple, dict] = {}
    for r in rows:
        a = agg.setdefault((r.strong_model, r.cheap_model), {"strong": r.strong_model, "cheap": r.cheap_model, "runs": set(),
                                                             "passed": 0, "failed": 0, "skipped": 0, "cost": 0.0, "priced": True,
                                                             "tokens": 0, "last_run": r.created_at})
        a["runs"].add(r.run_id)
        a[r.status.lower()] += 1
        a["tokens"] += r.input_tokens + r.output_tokens
        if r.cost_usd is None:
            a["priced"] = False
        else:
            a["cost"] += r.cost_usd
    out = []
    for a in agg.values():
        n = a["passed"] + a["failed"] + a["skipped"]
        scored = a["passed"] + a["failed"]
        out.append({"strong": a["strong"], "cheap": a["cheap"], "runs": len(a["runs"]), "scenarios": n,
                    "pass_rate": round(a["passed"] / scored, 3) if scored else None, "skipped": a["skipped"],
                    "avg_tokens": round(a["tokens"] / n) if n else 0,
                    "avg_cost_usd": round(a["cost"] / n, 5) if n and a["priced"] else None, "last_run": a["last_run"].isoformat() + "Z"})
    return out
