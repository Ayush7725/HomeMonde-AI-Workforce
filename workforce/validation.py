"""Deterministic guard layer between the model and a trusted finding.

A finding is only accepted when its claims are grounded: facts and observations must cite evidence,
every number written in a claim must appear (to its stated precision) in the evidence it cites, stale
evidence downgrades the result, and recommendations must name a permitted action with valid parameters."""
import re

from .tenancy import TenantConfig
from .tools import numbers_in

KINDS = ["FACT", "OBSERVATION", "INFERENCE", "HYPOTHESIS"]
QUALITY = ["CONFIRMED", "LIKELY", "POSSIBLE", "INSUFFICIENT_EVIDENCE", "DATA_CONFLICT", "DATA_STALE"]
LADDER = ["INSUFFICIENT_EVIDENCE", "POSSIBLE", "LIKELY", "CONFIRMED"]  # weakest to strongest
STRONG_ENOUGH_TO_ACT = ("CONFIRMED", "LIKELY")
OUTCOMES = ["FINDING", "INSUFFICIENT_EVIDENCE", "DATA_CONFLICT", "DATA_STALE", "OUT_OF_SCOPE"]
ESCALATE = "NEEDS_INVESTIGATION"  # cheap tier only: the question needs the strong model
NUMBER = re.compile(r"(?<![\w\-/.:#])[-+]?\d[\d,]*(?:\.\d+)?(?![\w\-/:])")
FREE_INTEGERS = 5  # small counts ("3 contributing factors") need no citation


def output_schema(action_names: list[str], escalate: bool = False) -> dict:
    outcomes = OUTCOMES + ([ESCALATE] if escalate else [])
    return {
        "type": "object", "additionalProperties": False,
        "required": ["outcome", "headline", "statements", "evidence_quality", "impact", "unknowns", "recommendation"],
        "properties": {
            "outcome": {"type": "string", "enum": outcomes},
            "headline": {"type": "string", "description": "One plain sentence a manager can act on. No numbers that are not in cited evidence."},
            "statements": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["kind", "text", "refs"],
                "properties": {"kind": {"type": "string", "enum": KINDS}, "text": {"type": "string"},
                               "refs": {"type": "array", "items": {"type": "string"}}}}},
            "evidence_quality": {"type": "string", "enum": QUALITY},
            "impact": {"type": "string"},
            "unknowns": {"type": "array", "items": {"type": "string"}},
            "recommendation": {"type": "object", "additionalProperties": False,
                               "required": ["action", "summary", "rationale", "params", "refs"],
                               "properties": {
                                   "action": {"type": "string", "enum": ["none", *action_names]},
                                   "summary": {"type": "string"}, "rationale": {"type": "string"},
                                   "params": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                              "required": ["name", "value"],
                                              "properties": {"name": {"type": "string"}, "value": {"type": "string"}}}},
                                   "refs": {"type": "array", "items": {"type": "string"}}}},
        },
    }


def grounded(num_text: str, pool: list[float], free_small: bool = True) -> bool:
    s = num_text.replace(",", "").lstrip("+")
    try:
        v = float(s)
    except ValueError:
        return True
    if free_small and v.is_integer() and 0 <= v <= FREE_INTEGERS:
        return True
    decimals = len(s.split(".")[1]) if "." in s else 0
    for n in pool:
        for cand in (n, n * 100, abs(n), abs(n) * 100):  # ratios may be written as percentages
            if round(cand, decimals) == round(v, decimals) or abs(cand - v) <= max(1e-6, abs(cand) * 0.005):
                return True
    return False


def check(finding: dict, evidence: dict[str, dict], cfg: TenantConfig, employee: str) -> tuple[dict, list[str]]:
    """Returns (cleaned finding, problems). Unsupported claims are removed, never passed on."""
    problems: list[str] = []
    kept = []
    for i, st in enumerate(finding.get("statements", [])):
        kind, text, refs = st.get("kind"), str(st.get("text", "")).strip(), [r for r in st.get("refs", []) if isinstance(r, str)]
        bad_refs = [r for r in refs if r not in evidence]
        if bad_refs:
            problems.append(f"statement {i + 1} cites unknown refs {bad_refs}")
            continue
        if kind in ("FACT", "OBSERVATION") and not refs:
            problems.append(f"statement {i + 1} ({kind}) has no evidence ref")
            continue
        pool = [n for r in refs for n in numbers_in(evidence[r]["result"])]
        ungrounded = [m for m in NUMBER.findall(text) if not grounded(m, pool)]
        if ungrounded:
            problems.append(f"statement {i + 1}: numbers {ungrounded} are not in its cited evidence {refs or '(none)'}; "
                            "cite the evidence or use the calculate tool")
            continue
        kept.append({"kind": kind, "text": text, "refs": refs})

    # "Nothing found" is not proof of absence when the search used a value no record has (e.g. material_code "ALL"):
    # such a statement is only the AI's reading, and the answer says what it could not check.
    unmatched = {r for r, e in evidence.items() if isinstance(e.get("result"), dict) and e["result"].get("unmatched_filters")}
    blind = []
    for i, st in enumerate(kept):
        if st["kind"] in ("FACT", "OBSERVATION") and set(st["refs"]) <= unmatched:
            problems.append(f"statement {i + 1} rests only on searches that found nothing because their filter values do not "
                            f"exist ({', '.join(st['refs'])}); use a tool that lists the records, or say it is not known")
            st["kind"] = "INFERENCE"
            blind += st["refs"]

    all_pool = [n for e in evidence.values() for n in numbers_in(e["result"])]
    headline = str(finding.get("headline", "")).strip()
    if [m for m in NUMBER.findall(headline) if not grounded(m, all_pool)]:
        problems.append("headline contains numbers that are not in the evidence")
        headline = ""

    outcome = finding.get("outcome") if finding.get("outcome") in OUTCOMES + [ESCALATE] else "INSUFFICIENT_EVIDENCE"
    quality = finding.get("evidence_quality") if finding.get("evidence_quality") in QUALITY else "INSUFFICIENT_EVIDENCE"
    cited = {r for s in kept for r in s["refs"]}
    if any(evidence[r]["freshness"] != "CURRENT" for r in cited):
        quality, outcome = "DATA_STALE", ("DATA_STALE" if outcome == "FINDING" else outcome)
    if outcome == "FINDING" and not any(s["kind"] == "FACT" for s in kept):
        problems.append("a FINDING needs at least one grounded FACT")
        outcome, quality = "INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE"
    quality, basis = certainty(quality, kept, evidence, cfg)

    rec = finding.get("recommendation") or {}
    action = rec.get("action", "none")
    clean_rec = None
    if action and action != "none" and outcome == "FINDING":
        allowed = cfg.employees[employee].actions
        if action not in allowed:
            problems.append(f"recommendation uses action {action} which {employee} may not propose")
        else:
            spec = cfg.actions[action].params
            raw = {p.get("name"): p.get("value") for p in rec.get("params", []) if isinstance(p, dict)}
            params, perr = {}, []
            for name, ps in spec.items():
                v = raw.get(name)
                if v in (None, ""):
                    if ps.required:
                        perr.append(f"missing {name}")
                    continue
                try:
                    v = int(float(v)) if ps.type == "integer" else float(v) if ps.type == "number" else str(v)[:300]
                except ValueError:
                    perr.append(f"{name} must be a {ps.type}")
                    continue
                if ps.enum and v not in ps.enum:
                    perr.append(f"{name} must be one of {ps.enum}")
                if isinstance(v, (int, float)) and ((ps.minimum is not None and v < ps.minimum) or (ps.maximum is not None and v > ps.maximum)):
                    perr.append(f"{name} out of range")
                params[name] = v
            extra = set(raw) - set(spec)
            if extra:
                perr.append(f"unknown params {sorted(extra)}")
            rrefs = [r for r in rec.get("refs", []) if r in evidence]
            # Text the person will send or file (e.g. a supplier message) may only carry numbers from the cited evidence.
            rpool = [n for r in rrefs for n in numbers_in(evidence[r]["result"])]
            for name, v in params.items():
                if isinstance(v, str):
                    loose = [m for m in NUMBER.findall(v) if not grounded(m, rpool)]
                    if loose:
                        perr.append(f"{name} has numbers {loose} that are not in the cited evidence")
            if perr:
                problems.append("recommendation: " + "; ".join(perr))
            elif not rrefs:
                problems.append("recommendation must cite the evidence it rests on")
            else:
                clean_rec = {"action": action, "summary": str(rec.get("summary", ""))[:300],
                             "rationale": str(rec.get("rationale", ""))[:1000], "params": params, "refs": rrefs}

    cleaned = {"outcome": outcome, "headline": headline or _fallback_headline(kept, outcome),
               "statements": kept, "evidence_quality": quality, "quality_basis": basis, "impact": str(finding.get("impact", ""))[:500],
               "unknowns": [str(u)[:300] for u in finding.get("unknowns", [])][:8], "recommendation": clean_rec}
    if blind:
        cleaned["unknowns"] = (cleaned["unknowns"] + [
            f"A search ({', '.join(sorted(set(blind)))}) found nothing because its filter value is not used in the data; "
            "that does not show that there are none."])[:9]
    if [m for m in NUMBER.findall(cleaned["impact"]) if not grounded(m, all_pool)]:
        problems.append("impact contains numbers that are not in the evidence")
        cleaned["impact"] = ""
    return cleaned, problems


def certainty(claimed: str, kept: list[dict], evidence: dict[str, dict], cfg: TenantConfig) -> tuple[str, list[str]]:
    """How sure the finding may be, judged from the evidence rather than taken from the model. The model's own rating can
    only be lowered, never raised:
      - facts only, all from current data           -> up to CONFIRMED
      - the AI also interprets (inference/hypothesis) -> up to LIKELY if the facts come from 2+ different datasets, else POSSIBLE
      - trusted sources disagree                      -> DATA_CONFLICT
      - any cited evidence out of date                -> DATA_STALE (set before this)
    Returns the rating and plain-language reasons."""
    facts = [s for s in kept if s["kind"] in ("FACT", "OBSERVATION")]
    reading = [s for s in kept if s["kind"] in ("INFERENCE", "HYPOTHESIS")]
    cited = {r for s in kept for r in s["refs"]}
    datasets = sorted({d for s in facts for r in s["refs"] for d in evidence[r].get("datasets", [])})
    labels = [cfg.datasets[d].label if d in cfg.datasets else d for d in datasets]
    basis = []
    if datasets:
        basis.append(f"The facts come from {len(datasets)} kind{'s' if len(datasets) != 1 else ''} of business records: {', '.join(labels)}")
    if reading:
        basis.append(f"{len(reading)} statement{'s are' if len(reading) != 1 else ' is'} the AI's interpretation, not read directly from the data")
    if claimed == "DATA_STALE":
        basis.append("Some of the data used is out of date")
        return claimed, basis
    if any(evidence[r].get("name") == "source_conflict" for r in cited):
        basis.append("Two trusted sources disagree, so the figures cannot be relied on until that is fixed")
        return "DATA_CONFLICT", basis
    if claimed not in LADDER:
        return claimed, basis
    if not facts:
        ceiling = "INSUFFICIENT_EVIDENCE"
    elif not reading:
        ceiling = "CONFIRMED"
    else:
        ceiling = "LIKELY" if len(datasets) >= 2 else "POSSIBLE"
    final = min(claimed, ceiling, key=LADDER.index)
    if LADDER.index(final) < LADDER.index(claimed):
        why = "it rests on interpretation backed by only one kind of record" if ceiling == "POSSIBLE" else \
              "part of it is interpretation" if ceiling == "LIKELY" else "no fact supports it"
        basis.append(f"Lowered from {claimed.lower().replace('_', ' ')} to {final.lower().replace('_', ' ')} because {why}")
    return final, basis


def _fallback_headline(statements: list[dict], outcome: str) -> str:
    facts = [s["text"] for s in statements if s["kind"] == "FACT"]
    if facts:
        return facts[0]
    return {"OUT_OF_SCOPE": "Outside this workforce's business scope",
            "DATA_STALE": "Required data is not current", "DATA_CONFLICT": "Trusted sources disagree"}.get(outcome, "Insufficient evidence to reach a conclusion")
