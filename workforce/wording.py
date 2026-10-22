"""Rewording of already-verified text by the cheap model (briefs, notifications, summaries).

The cheap model never adds facts. Every line it writes goes through the same number check as findings:
a line containing a number that is not in its verified source is rejected and the verified wording is used.
If any line fails, the whole batch is retried once on the strong model before falling back line by line."""
import json
import logging

from sqlalchemy.orm import Session

from .llm import LLM, LLMRefused, LLMRetryable, LLMUnavailable
from .tenancy import TenantConfig
from .validation import NUMBER

log = logging.getLogger(__name__)

SYSTEM = """You rewrite verified business statements from {company}'s AI workforce for {audience}.
Rules:
- Keep the meaning of each item. Do not add facts, causes, advice, dates or numbers that are not in that item.
- Copy every number you keep exactly as written. You may leave a number out; never change or round it.
- Plain, direct language. One sentence per item, at most {words} words.
{extra}"""

AUDIENCE = {"brief": "a management brief", "notification": "a short alert to a busy founder"}


def _schema(with_overview: bool) -> dict:
    props = {"lines": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "text"],
                                                  "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}}
    if with_overview:
        props["overview"] = {"type": "string"}
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def _values(text: str) -> set[float]:
    out = set()
    for n in NUMBER.findall(text):
        try:
            out.add(float(n.replace(",", "").lstrip("+")))
        except ValueError:
            pass
    return out


def numbers_ok(text: str, source: str) -> bool:
    """Rewording must copy numbers exactly: every number in text must appear, unrounded, in its verified source."""
    return _values(text) <= _values(source)


def _attempt(db, llm: LLM, tier: str, task: str, cfg: TenantConfig, items: list[dict], overview: bool, employee: str):
    extra = "- Also write overview: two sentences summarising the items, using only their numbers." if overview else ""
    system = SYSTEM.format(company=cfg.name, audience=AUDIENCE[task], words=30 if task == "brief" else 25, extra=extra)
    user = json.dumps({"items": [{"id": i["id"], "text": i["text"]} for i in items]}, separators=(",", ":"))
    reply = llm.create(db, tier=tier, task=task, work_id=None, employee=employee, system=system, tools=[],
                       messages=[{"role": "user", "content": user}], schema=_schema(overview), effort="low")
    try:
        out = json.loads(_json_text(reply.text))
    except ValueError:
        return {}, None, len(items)
    by_id = {i["id"]: i["text"] for i in items}
    lines, rejected = {}, 0
    for line in out.get("lines", []) if isinstance(out, dict) else []:
        iid, text = str(line.get("id")), str(line.get("text", "")).strip()
        if iid in by_id and text and numbers_ok(text, by_id[iid]):
            lines[iid] = text[:400]
    rejected = len([i for i in items if i["id"] not in lines])
    ov = str(out.get("overview", "")).strip() if overview and isinstance(out, dict) else None
    if ov and not numbers_ok(ov, " ".join(by_id.values())):
        ov, rejected = None, rejected + 1
    return lines, ov, rejected


def _json_text(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").split("\n", 1)[-1]
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if start >= 0 and end > start else t


def reword(db: Session, cfg: TenantConfig, items: list[dict], task: str, *, overview: bool = False,
           employee: str = "system") -> dict:
    """items: [{"id", "text"}] of verified wording. Returns {"lines": {id: text}, "overview", "model", "rejected", "fallback"}.
    Commits the session before each model call, so call it only where earlier work in the session may be saved."""
    result = {"lines": {i["id"]: i["text"] for i in items}, "overview": None, "model": None, "rejected": 0, "fallback": True}
    if not items:
        return result
    llm = LLM.load(db, cfg)
    cfg = llm.cfg  # models as currently chosen by the owners
    tiers = [cfg.ai.tier_for(task)]
    if tiers[0] == "cheap":
        tiers.append("strong")
    accepted, overview_text, used = {}, None, []
    for tier in tiers:
        if llm.unavailable_reason(db, tier):
            continue
        db.commit()  # never hold a transaction (and its locks) open while the model is working
        try:
            lines, ov, rejected = _attempt(db, llm, tier, task, cfg, items, overview, employee)
        except (LLMRetryable, LLMRefused, LLMUnavailable) as e:
            log.info("rewording on %s tier failed: %s", tier, e)
            continue
        used.append(f"{getattr(cfg.ai, tier).provider}:{getattr(cfg.ai, tier).model}")
        accepted.update(lines)  # a later (stronger) attempt's accepted lines win
        overview_text = ov or overview_text
        if rejected == 0:
            break
    if not used:
        return result
    result["lines"].update(accepted)
    missing = len([i for i in items if i["id"] not in accepted]) + (1 if overview and not overview_text else 0)
    result.update(overview=overview_text, model=" then ".join(used), rejected=missing, fallback=False)
    return result
