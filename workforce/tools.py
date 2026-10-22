"""Controlled tools: the AI's only window onto business data. Every call is permission-checked against
the employee's config, executed deterministically, compacted, stored as citable evidence and audited."""
import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import data, events, expr, health, work
from .metrics import MetricEngine
from .models import Evidence, WorkItem
from .tenancy import ParamSpec, TenantConfig, ToolConfig

CALC = "calculate"
ASK = "ask_"  # colleague tools are exposed as ask_<employee>__<tool>


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    db: Session
    cfg: TenantConfig
    root: WorkItem       # the investigation evidence belongs to
    employee: str        # who is calling
    engine: MetricEngine


def _schema(params: dict[str, ParamSpec]) -> dict:
    props = {}
    for name, p in params.items():
        s: dict = {"type": p.type}
        if p.description:
            s["description"] = p.description
        if p.enum:
            s["enum"] = p.enum
        props[name] = s
    return {"type": "object", "properties": props, "required": [n for n, p in params.items() if p.required],
            "additionalProperties": False}


def definitions(cfg: TenantConfig, employee: str) -> list[dict]:
    """Tool list for one employee, in a stable order so the prompt prefix stays cacheable."""
    emp = cfg.employees[employee]
    defs = [{"name": t, "description": cfg.tools[t].description, "input_schema": _schema(cfg.tools[t].params), "strict": True}
            for t in sorted(emp.tools)]
    for colleague in sorted(emp.can_ask):
        c = cfg.employees[colleague]
        for t in sorted(c.tools):
            defs.append({"name": f"{ASK}{colleague}__{t}"[:64],
                         "description": f"Ask {c.name} ({c.title}) to run its tool: {cfg.tools[t].description}",
                         "input_schema": _schema(cfg.tools[t].params), "strict": True})
    defs.append({"name": CALC, "strict": True,
                 "description": "Deterministic calculator. Use it for every derived number (differences, ratios, percentages, "
                                "projections) instead of computing in your head. Inputs must be numbers taken from cited evidence.",
                 "input_schema": {"type": "object", "additionalProperties": False, "required": ["expression", "label", "inputs"],
                                  "properties": {
                                      "expression": {"type": "string", "description": "Arithmetic over the input names, e.g. (a - b) / b * 100"},
                                      "label": {"type": "string", "description": "What the result means"},
                                      "inputs": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                 "required": ["name", "value", "ref"],
                                                 "properties": {"name": {"type": "string"}, "value": {"type": "number"},
                                                                "ref": {"type": "string", "description": "Evidence ref the value came from"}}}}}}})
    return defs


def _check_params(spec: dict[str, ParamSpec], args: dict) -> dict:
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    unknown = set(args) - set(spec)
    if unknown:
        raise ToolError(f"unknown arguments {sorted(unknown)}")
    clean = {}
    for name, p in spec.items():
        v = args.get(name)
        if v is None:
            if p.required:
                raise ToolError(f"missing required argument {name}")
            continue
        if p.type == "string":
            v = str(v)[:200]
            if p.enum and v not in p.enum:
                raise ToolError(f"{name} must be one of {p.enum}")
        else:
            try:
                v = int(v) if p.type == "integer" else float(v)
            except (TypeError, ValueError):
                raise ToolError(f"{name} must be a {p.type}") from None
            if (p.minimum is not None and v < p.minimum) or (p.maximum is not None and v > p.maximum):
                raise ToolError(f"{name} out of range")
        clean[name] = v
    return clean


def next_ref(db: Session, tenant_id: str, work_id: str, prefix: str) -> str:
    n = db.execute(select(func.count()).select_from(Evidence).where(Evidence.tenant_id == tenant_id, Evidence.work_id == work_id,
                                                                    Evidence.ref.like(f"{prefix}%"))).scalar()
    return f"{prefix}{n + 1}"


MAX_RECORD_IDS = 500


def store(ctx: ToolContext, kind: str, prefix: str, employee: str, name: str, args: dict, result, freshness: str = "CURRENT",
          source: str = "", record_ids: list[int] | None = None) -> Evidence:
    ids = sorted(set(record_ids or []))
    ev = Evidence(tenant_id=ctx.cfg.id, work_id=ctx.root.id, ref=next_ref(ctx.db, ctx.cfg.id, ctx.root.id, prefix), kind=kind, employee=employee,
                  name=name, args=args, result=result, freshness=freshness, source=source,
                  record_ids=ids[:MAX_RECORD_IDS], record_count=len(ids))
    ctx.db.add(ev)
    ctx.db.flush()
    return ev


def _run_config_tool(ctx: ToolContext, tname: str, tool: ToolConfig, args: dict) -> tuple[dict, str, str, list[int]]:
    cfg = ctx.cfg
    if tool.kind == "lookup":
        ds = cfg.datasets[tool.dataset]
        fresh, synced = data.freshness(ctx.db, cfg, tool.dataset)
        every = data.rows(ctx.db, cfg, tool.dataset, with_ids=True)
        found = [(rid, r) for rid, r in every if all(str(r.get(k)) == str(v) for k, v in args.items())]
        if tool.order_by:
            found.sort(key=lambda p: (p[1].get(tool.order_by) is None, p[1].get(tool.order_by)), reverse=False)
        total = len(found)
        out_rows = [{f: r.get(f) for f in tool.fields} for _, r in found[:tool.limit]]
        result = {"rows": out_rows, "total_matches": total}
        if total > tool.limit:
            result["truncated"] = True
        if not total and args:
            # Nothing found. Say whether each filter value exists at all: "ALL" or a guessed code matches nothing, and
            # that proves nothing about whether such records exist.
            unmatched = sorted(k for k, v in args.items() if not any(str(r.get(k)) == str(v) for _, r in every))
            if unmatched:
                result["unmatched_filters"] = unmatched
                result["note"] = (f"No record has this {' / '.join(unmatched)} value, so this search does not show whether "
                                  "matching records exist. Use exact values from earlier results, or a tool that lists them.")
            else:
                result["note"] = "No record matches all of these filters together."
        return result, fresh, f"{ds.label} · synced {synced}", [rid for rid, _ in found]
    # metric tool
    datasets = set()
    out = {}
    for m in tool.metrics:
        mc = cfg.metrics[m]
        if mc.dataset:
            datasets.add(mc.dataset)
        rows = ctx.engine.lookup(m, args)
        head = metric_header(cfg, m)
        if tool.breakdown and tool.breakdown in mc.by:
            out[m] = {**head, "by_" + tool.breakdown: {str(r[tool.breakdown]): r["value"] for r in rows}}
        elif len(rows) == 1:
            out[m] = {**head, "value": rows[0]["value"]}
        else:
            rows.sort(key=lambda r: (r["value"] is None, r["value"] or 0), reverse=tool.order == "desc")
            out[m] = {**head, "rows": rows[:tool.limit], "total_groups": len(rows)}
    states = [data.freshness(ctx.db, cfg, d) for d in sorted(_metric_datasets(cfg, tool.metrics))]
    fresh = "STALE" if any(s[0] != "CURRENT" for s in states) else "CURRENT"
    ids = [rid for m in tool.metrics for rid in ctx.engine.record_ids(m, args)]
    return ({"metrics": out}, fresh,
            "Metric registry · " + ", ".join(f"{cfg.datasets[d].label} synced {s[1]}" for d, s in zip(sorted(_metric_datasets(cfg, tool.metrics)), states)),
            ids)


def metric_header(cfg: TenantConfig, name: str) -> dict:
    """Definition facts the AI may cite alongside a metric value (label, unit, version, window)."""
    mc = cfg.metrics[name]
    head = {"label": mc.label, "unit": mc.unit, "version": mc.version}
    if mc.window_days:
        head["window_days"] = mc.window_days
    return head


def _metric_datasets(cfg: TenantConfig, names: list[str]) -> set[str]:
    out, todo = set(), list(names)
    while todo:
        mc = cfg.metrics[todo.pop()]
        if mc.dataset:
            out.add(mc.dataset)
        else:
            todo.extend(expr.names(mc.formula))
    return out


def _summary(result) -> str:
    s = json.dumps(result, separators=(",", ":"), default=str)
    return s if len(s) <= 220 else s[:217] + "…"


def call(ctx: ToolContext, name: str, raw_args: dict) -> dict:
    """Execute one tool call requested by the model. Returns the compact payload sent back to it."""
    cfg, db = ctx.cfg, ctx.db
    emp = cfg.employees[ctx.employee]
    if name == CALC:
        return _calculate(ctx, raw_args)
    target_emp, tname = ctx.employee, name
    if name.startswith(ASK) and "__" in name:
        target_emp, tname = name[len(ASK):].split("__", 1)
        if target_emp not in emp.can_ask or tname not in cfg.employees.get(target_emp, emp).tools:
            raise ToolError(f"{emp.name} is not allowed to use {name}")
    elif name not in emp.tools:
        raise ToolError(f"{emp.name} is not allowed to use {name}")
    tool = cfg.tools[tname]
    args = _check_params(tool.params, raw_args)
    argtxt = ", ".join(f"{k}={v}" for k, v in args.items())

    child = None
    if target_emp != ctx.employee:
        try:
            health.ensure_running(db, cfg.id, target_emp)
        except health.EmployeePaused:
            raise ToolError(f"{cfg.employees[target_emp].name} is paused and cannot take requests") from None
        colleague = cfg.employees[target_emp]
        events.emit(db, cfg.id, "AI_REQUEST", f"{emp.name} → {colleague.name}: {tname}({argtxt}) for {ctx.root.id}",
                    actor=ctx.employee, work_id=ctx.root.id, emp=ctx.employee, to=target_emp, tool=tname)
        child = work.create(db, cfg, employee=target_emp, type_="COLLEAGUE_REQUEST", priority=ctx.root.priority,
                            title=f"{tname.replace('_', ' ').capitalize()} for {emp.name}", subject=argtxt, origin="request",
                            parent_id=ctx.root.id, context={"asked_by": ctx.employee, "tool": tname, "args": args},
                            next_step=f"Run {tname}")
        work.transition(db, cfg, child, "RUNNING", f"Run {tname}")

    events.emit(db, cfg.id, "TOOL_CALLED", f"{cfg.employees[target_emp].name} calls {tname}({argtxt})",
                actor=target_emp, work_id=(child or ctx.root).id, emp=target_emp, tool=tname, area=tool.area, spot=tool.spot,
                root=ctx.root.id)
    try:
        result, fresh, source, record_ids = _run_config_tool(ctx, tname, tool, args)
    except Exception as e:
        events.emit(db, cfg.id, "TOOL_ERROR", f"{tname} failed: {str(e)[:200]}. No result was invented.",
                    actor=target_emp, work_id=(child or ctx.root).id, emp=target_emp, tool=tname)
        if child:
            work.transition(db, cfg, child, "FAILED", "Tool failed", outcome="INVESTIGATION_FAILED", error=str(e))
        raise ToolError(f"{tname} failed") from e
    ev = store(ctx, "tool", "E", target_emp, tname, args, result, fresh, source, record_ids)
    events.emit(db, cfg.id, "TOOL_RESULT", f"{ev.ref} · {tname}: {_summary(result)} · {fresh}",
                actor=target_emp, work_id=(child or ctx.root).id, emp=target_emp, tool=tname, ref=ev.ref, freshness=fresh)
    if child:
        work.transition(db, cfg, child, "COMPLETED", f"Returned to {emp.name}", outcome="ANSWERED")
    payload = {"ref": ev.ref, "freshness": fresh, "source": source, "result": result}
    if fresh != "CURRENT":
        payload["warning"] = "Data is not current. Do not present it as the current position."
    return payload


def _calculate(ctx: ToolContext, raw: dict) -> dict:
    expression, label, inputs = raw.get("expression"), str(raw.get("label", ""))[:120], raw.get("inputs") or []
    if not isinstance(inputs, list) or not inputs or len(inputs) > 12:
        raise ToolError("inputs must list 1-12 named values with their evidence refs")
    known = {e.ref: e for e in ctx.db.execute(select(Evidence).where(Evidence.tenant_id == ctx.cfg.id, Evidence.work_id == ctx.root.id)).scalars()}
    env, problems = {}, []
    for i in inputs:
        name, value, ref = str(i.get("name", "")), i.get("value"), str(i.get("ref", ""))
        if ref not in known:
            problems.append(f"unknown ref {ref}")
            continue
        if not isinstance(value, (int, float)) or not _value_in(value, known[ref].result):
            problems.append(f"{name}={value} does not appear in {ref}")
            continue
        env[name] = value
    if problems:
        raise ToolError("; ".join(problems))
    try:
        unknown = expr.names(expression) - set(env)
        if unknown:
            raise ToolError(f"expression uses undefined names {sorted(unknown)}")
        value = expr.evaluate(expression, env)
    except expr.ExprError as e:
        raise ToolError(str(e)) from None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ToolError("calculation is undefined for these inputs (for example division by zero)")
    value = round(float(value), 4)
    stale = any(known[str(i.get('ref'))].freshness != "CURRENT" for i in inputs)
    ev = store(ctx, "calc", "C", ctx.employee, label or "calculation",
               {"expression": expression, "inputs": {k: v for k, v in env.items()}, "refs": sorted({str(i['ref']) for i in inputs})},
               {"value": value, "label": label}, "STALE" if stale else "CURRENT", "Calculation service")
    events.emit(ctx.db, ctx.cfg.id, "CALCULATION", f"{ev.ref} · {label} = {value:g} ({expression})",
                actor=ctx.employee, work_id=ctx.root.id, emp=ctx.employee, ref=ev.ref)
    return {"ref": ev.ref, "value": value, "label": label}


def numbers_in(obj) -> list[float]:
    out = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(numbers_in(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(numbers_in(v))
    elif isinstance(obj, str):
        try:
            out.append(float(obj))
        except ValueError:
            pass
    return out


def _value_in(value: float, result) -> bool:
    return any(abs(n - value) <= max(1e-6, abs(n) * 1e-4) for n in numbers_in(result))
