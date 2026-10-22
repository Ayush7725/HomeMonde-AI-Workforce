"""Daily, weekly and monthly briefs (Locks 12 and 14), built from verified findings only.

Every line starts as verified wording (finding headline plus its first FACT, decisions, data health). The cheap
model may reword lines and write a short overview; anything that fails the number check keeps the verified text."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import data, events, health, wording
from .db import utcnow
from .models import Approval, Event, LlmUsage, Report, User, WorkItem
from .tenancy import TenantConfig

SPAN = {"daily": timedelta(days=1), "weekly": timedelta(days=7), "monthly": timedelta(days=30)}
WEEKDAY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
URGENT = ("CRITICAL", "HIGH")
DECIDED = {"APPROVED": "approved", "MODIFIED": "approved with changes", "REJECTED": "said no to",
           "MORE_INVESTIGATION": "asked for more checks on", "EXPIRED": "let expire"}
STATUS_WORDS = {"DEGRADED": "having problems", "FAILED": "not working", "PAUSED": "paused", "NEEDS_ATTENTION": "needs attention"}


def due(db: Session, cfg: TenantConfig) -> list[str]:
    """Which reports are due now in the company's own timezone. Each kind is made once per period."""
    if not cfg.reports.enabled:
        return []
    local = datetime.now(ZoneInfo(cfg.timezone))
    if local.hour < cfg.reports.daily_hour:
        return []
    today = local.date()
    kinds = ["daily"]
    if WEEKDAY[today.weekday()] == cfg.reports.weekly_day:
        kinds.append("weekly")
    if today.day == cfg.reports.monthly_day:
        kinds.append("monthly")
    made = set(db.execute(select(Report.kind).where(Report.tenant_id == cfg.id, Report.local_date == today)).scalars())
    return [k for k in kinds if k not in made]


def _finding_line(item: WorkItem) -> str:
    f = item.finding or {}
    fact = next((s["text"] for s in f.get("statements", []) if s["kind"] == "FACT"), "")
    return f"{f.get('headline') or item.title}. {fact}".strip().rstrip(".") + "."


def generate(db: Session, cfg: TenantConfig, kind: str) -> Report:
    end = utcnow()
    start = end - SPAN[kind]
    items = db.execute(select(WorkItem).where(WorkItem.tenant_id == cfg.id, WorkItem.parent_id.is_(None),
                                              WorkItem.updated_at >= start).order_by(WorkItem.updated_at.desc())).scalars().all()
    findings = [i for i in items if i.finding and i.finding.get("outcome") not in ("OUT_OF_SCOPE", None)]
    pending = db.execute(select(Approval).where(Approval.tenant_id == cfg.id, Approval.status == "PENDING")).scalars().all()
    decided = db.execute(select(Approval).where(Approval.tenant_id == cfg.id, Approval.decided_at >= start)).scalars().all()
    users = {u.id: u.name for u in db.execute(select(User).where(User.tenant_id == cfg.id)).scalars()}

    def entry(i: WorkItem) -> dict:
        f = i.finding
        return {"id": i.id, "work_id": i.id, "employee": i.employee, "priority": i.priority, "quality": f.get("evidence_quality"),
                "verified": _finding_line(i), "text": _finding_line(i)}

    good = [entry(i) for i in findings if i.context.get("sentiment") == "opportunity"]
    problems = [i for i in findings if i.context.get("sentiment") != "opportunity"]
    attention = [entry(i) for i in problems if i.priority in URGENT or (i.finding or {}).get("recommendation")]
    other = [entry(i) for i in problems if not (i.priority in URGENT or (i.finding or {}).get("recommendation"))]
    waiting = [{"id": a.id, "work_id": a.work_id, "employee": a.employee, "priority": "HIGH", "quality": None,
                "verified": f"Waiting for a decision: {a.title}" + (f" ({cfg.currency} {a.amount:,.0f})." if a.amount else "."), "text": None}
               for a in pending]
    made = [{"id": a.id, "work_id": a.work_id, "employee": a.employee, "priority": None, "quality": None,
             "verified": f"{users.get(a.decided_by, 'Someone')} {DECIDED.get(a.status, a.status.lower())}: {a.title}.", "text": None}
            for a in decided]
    for e in waiting + made:
        e["text"] = e["verified"]

    data_rows = []
    for sid, src in cfg.sources.items():
        states = {data.freshness(db, cfg, d)[0] for d, ds in cfg.datasets.items() if ds.source == sid}
        if states - {"CURRENT"}:
            line = f"{src.label or sid} has not sent new data recently, so its figures may be out of date."
            data_rows.append({"id": f"src-{sid}", "work_id": None, "employee": None, "priority": None, "quality": None,
                              "verified": line, "text": line})
    rejected = db.execute(select(func.count()).select_from(Event).where(Event.tenant_id == cfg.id, Event.type == "DATA_REJECTED",
                                                                        Event.ts >= start)).scalar()
    if rejected:
        line = f"{rejected} data files had rows with mistakes; those rows were left out."
        data_rows.append({"id": "rejected", "work_id": None, "employee": None, "priority": None, "quality": None, "verified": line, "text": line})

    workforce_rows = []
    for emp, h in health.employee_health(db, cfg, set(cfg.employees)).items():
        if h["status"] != "HEALTHY":
            line = f"{cfg.employees[emp].name} is {STATUS_WORDS.get(h['status'], h['status'].lower())}: {'; '.join(h['reasons'])}."
            workforce_rows.append({"id": f"emp-{emp}", "work_id": None, "employee": emp, "priority": None, "quality": None,
                                   "verified": line, "text": line})

    usage = [{"provider": p, "model": m, "calls": n, "tokens": int(t or 0), "cost_usd": round(c, 4) if c is not None else None}
             for p, m, n, t, c in db.execute(select(LlmUsage.provider, LlmUsage.model, func.count(),
                                                    func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens), func.sum(LlmUsage.cost_usd))
                                             .where(LlmUsage.tenant_id == cfg.id, LlmUsage.created_at >= start)
                                             .group_by(LlmUsage.provider, LlmUsage.model))]

    to_reword = attention + good + other
    polished = wording.reword(db, cfg, [{"id": e["id"], "text": e["verified"]} for e in to_reword], "brief",
                              overview=bool(to_reword), employee=cfg.question_employee())
    for e in to_reword:
        e["text"] = polished["lines"].get(e["id"], e["verified"])
    overview = polished["overview"] or (f"{len(attention)} items need attention, {len(waiting)} decisions are waiting, "
                                        f"{len(good)} good news and {len(other)} other findings.")
    local_today = datetime.now(ZoneInfo(cfg.timezone)).date()
    rid = f"RPT-{kind[0].upper()}-{local_today.isoformat()}"
    content = {
        "overview": overview,
        "sections": [
            {"key": "attention", "title": "Needs attention", "items": attention},
            {"key": "waiting", "title": "Decisions waiting", "items": waiting},
            {"key": "good", "title": "Good news", "items": good},
            {"key": "decided", "title": "Decisions made", "items": made},
            {"key": "other", "title": "Other findings", "items": other},
            {"key": "data", "title": "Data health", "items": data_rows},
            {"key": "workforce", "title": "Workforce health", "items": workforce_rows},
        ],
        "ai_usage": usage,
        "wording": {"model": polished["model"], "rejected_lines": polished["rejected"], "verified_only": polished["fallback"]},
    }
    report = db.get(Report, (cfg.id, rid)) or Report(tenant_id=cfg.id, id=rid)
    db.add(report)
    report.kind, report.period_start, report.period_end, report.local_date, report.content = kind, start, end, local_today, content
    db.flush()
    events.emit(db, cfg.id, "REPORT_GENERATED", f"{kind.capitalize()} brief {rid} ready · {len(attention)} need attention, "
                f"{len(waiting)} decisions waiting", actor=cfg.question_employee(), emp=cfg.question_employee(), report=rid)
    return report
