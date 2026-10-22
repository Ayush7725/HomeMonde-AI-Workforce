"""Operations for the AI workforce (Lock 17): pause switches (employees, action types, data feeds) and a
health view per AI employee."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import events
from .db import utcnow
from .models import Control, EvalResult, Heartbeat, LlmUsage, User, WorkItem
from .tenancy import TenantConfig


class EmployeePaused(Exception):
    pass


WORKER_LATE_AFTER = timedelta(minutes=3)


def beat(db: Session, name: str, started_at) -> None:
    """Record that a background process is alive and looping (an upsert, so concurrent workers never collide),
    and forget processes that have not beaten for a day (restarted containers)."""
    from sqlalchemy import delete
    from sqlalchemy.dialects import postgresql, sqlite

    from .db import is_postgres
    now = utcnow()
    ins = (postgresql.insert if is_postgres() else sqlite.insert)(Heartbeat).values(name=name, beat_at=now, started_at=started_at)
    db.execute(ins.on_conflict_do_update(index_elements=["name"], set_={"beat_at": now, "started_at": started_at}))
    db.execute(delete(Heartbeat).where(Heartbeat.beat_at < now - timedelta(days=1)))


def worker_status(db: Session) -> dict:
    """ok while some worker looped within WORKER_LATE_AFTER; late if the newest beat is older; missing if none ever ran."""
    last = db.execute(select(func.max(Heartbeat.beat_at)).where(Heartbeat.name.like("worker:%"))).scalar()
    if last is None:
        return {"status": "missing", "seconds_since_beat": None}
    age = (utcnow() - last).total_seconds()
    return {"status": "ok" if age <= WORKER_LATE_AFTER.total_seconds() else "late", "seconds_since_beat": round(age)}


KINDS = ("employee", "action", "source")


def paused_set(db: Session, tenant_id: str, kind: str = "employee") -> set[str]:
    return set(db.execute(select(Control.target).where(Control.tenant_id == tenant_id, Control.kind == kind, Control.paused)).scalars())


def ensure_running(db: Session, tenant_id: str, *employees: str) -> None:
    hit = paused_set(db, tenant_id) & set(employees)
    if hit:
        raise EmployeePaused(", ".join(sorted(hit)))


def set_paused(db: Session, cfg: TenantConfig, kind: str, target: str, paused: bool, user: User, reason: str = "") -> Control:
    """Pausing takes effect immediately. Employees stop at their next step; paused actions never execute;
    paused feeds are not synced. Every change is audited."""
    names = {"employee": {k: e.name for k, e in cfg.employees.items()}, "action": {k: a.label for k, a in cfg.actions.items()},
             "source": {k: (s.label or k) for k, s in cfg.sources.items()}}[kind]
    if target not in names:
        raise KeyError(target)
    ctl = db.get(Control, (cfg.id, kind, target)) or Control(tenant_id=cfg.id, kind=kind, target=target)
    db.add(ctl)
    ctl.paused, ctl.reason, ctl.changed_by, ctl.changed_at = paused, reason.strip()[:500], user.id, utcnow()
    verb = "paused" if paused else "resumed"
    what = {"employee": "", "action": "action ", "source": "data feed "}[kind]
    events.emit(db, cfg.id, "EMPLOYEE_PAUSED" if kind == "employee" and paused else "EMPLOYEE_RESUMED" if kind == "employee" else "CONTROL_CHANGED",
                f"{user.name} {verb} {what}{names[target]}" + (f" · {ctl.reason}" if ctl.reason else ""),
                actor=f"user:{user.id}", emp=target if kind == "employee" else None, control=kind, target=target, paused=paused, by=user.name)
    return ctl


def controls(db: Session, cfg: TenantConfig) -> dict[str, dict[str, dict]]:
    rows = {(c.kind, c.target): c for c in db.execute(select(Control).where(Control.tenant_id == cfg.id)).scalars()}
    out = {}
    for kind, items in (("employee", cfg.employees), ("action", cfg.actions), ("source", cfg.sources)):
        out[kind] = {}
        for k in items:
            c = rows.get((kind, k))
            out[kind][k] = {"paused": bool(c and c.paused), "reason": c.reason if c and c.paused else "",
                            "changed_at": c.changed_at.isoformat() + "Z" if c else None}
    return out


def employee_health(db: Session, cfg: TenantConfig, employees: set[str]) -> dict[str, dict]:
    """HEALTHY, DEGRADED, FAILED, PAUSED or NEEDS_ATTENTION, with the reasons and the numbers behind them."""
    from . import quality
    now = utcnow()
    day, week = now - timedelta(hours=24), now - timedelta(days=7)
    ctls = {c.target: c for c in db.execute(select(Control).where(Control.tenant_id == cfg.id, Control.kind == "employee")).scalars()}
    items = db.execute(select(WorkItem).where(WorkItem.tenant_id == cfg.id, WorkItem.parent_id.is_(None),
                                              WorkItem.created_at >= now - timedelta(days=30))).scalars().all()
    usage = {row[0]: row[1:] for row in db.execute(
        select(LlmUsage.employee, func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens), func.sum(LlmUsage.cost_usd))
        .where(LlmUsage.tenant_id == cfg.id, LlmUsage.created_at >= day).group_by(LlmUsage.employee))}
    accuracy = quality.accuracy(db, cfg.id)
    overrides = quality.override_rate(db, cfg.id)
    evals = _latest_eval(db, cfg.id)
    out = {}
    for emp in employees:
        mine = [w for w in items if w.employee == emp]
        open_ = [w for w in mine if w.status not in ("COMPLETED", "FAILED")]
        done = [w for w in mine if w.completed_at and w.status == "COMPLETED"]
        failed_24h = [w for w in mine if w.status == "FAILED" and w.completed_at and w.completed_at >= day]
        retrying = [w for w in open_ if w.status == "NEEDS_RETRY"]
        recent = [w for w in done if w.completed_at >= week and w.finding]
        noted = [w for w in recent if (w.finding or {}).get("validation_notes")]
        waiting = [w for w in open_ if w.status == "NEEDS_HUMAN_REVIEW" and now - w.updated_at > timedelta(hours=48)]
        last_ok = max((w.completed_at for w in done), default=None)
        ctl = ctls.get(emp)
        reasons = []
        if ctl and ctl.paused:
            status = "PAUSED"
            reasons.append(f"Paused by an owner: {ctl.reason or 'no reason given'}")
        elif len(failed_24h) >= 3 and not any(w.completed_at >= day for w in done):
            status = "FAILED"
            reasons.append(f"{len(failed_24h)} tasks could not be finished today and none were completed")
        else:
            status = "HEALTHY"
            if failed_24h or retrying:
                status = "DEGRADED"
                reasons.append(f"{len(failed_24h)} tasks could not be finished today; {len(retrying)} are being tried again")
            if recent and len(noted) / len(recent) > 0.3:
                status = "DEGRADED"
                reasons.append(f"the safety check removed unsupported statements in {len(noted)} of {len(recent)} findings this week")
            ov = overrides.get(emp)
            if ov and ov["decided"] >= 5 and ov["rate"] > 0.4:
                status = "NEEDS_ATTENTION" if status == "HEALTHY" else status
                reasons.append(f"people changed or turned down {ov['decided'] - ov['accepted']} of its last {ov['decided']} recommendations")
            if waiting or len(open_) > 20:
                status = "NEEDS_ATTENTION" if status == "HEALTHY" else status
                reasons.append(f"{len(waiting)} decisions have waited more than 2 days" if waiting else f"{len(open_)} tasks are waiting")
        tokens, spend = usage.get(emp, (0, None))
        acc = accuracy.get(emp)
        out[emp] = {"status": status, "reasons": reasons, "paused": bool(ctl and ctl.paused),
                    "paused_reason": ctl.reason if ctl and ctl.paused else "", "queue": len(open_),
                    "completed_7d": len([w for w in done if w.completed_at >= week]), "failed_24h": len(failed_24h),
                    "retrying": len(retrying), "validation_issue_rate_7d": round(len(noted) / len(recent), 3) if recent else None,
                    "last_success": last_ok.isoformat() + "Z" if last_ok else None, "tokens_24h": int(tokens or 0),
                    "cost_24h_usd": round(spend, 4) if spend is not None else None,
                    "accuracy": acc, "eval": evals.get(emp), "overrides": overrides.get(emp)}
    return out


def _latest_eval(db: Session, tenant_id: str) -> dict[str, dict]:
    run = db.execute(select(EvalResult.run_id).where(EvalResult.tenant_id == tenant_id).order_by(EvalResult.created_at.desc()).limit(1)).scalar()
    if not run:
        return {}
    out: dict[str, dict] = {}
    for r in db.execute(select(EvalResult).where(EvalResult.tenant_id == tenant_id, EvalResult.run_id == run)).scalars():
        e = out.setdefault(r.employee, {"run_id": run, "passed": 0, "failed": 0, "skipped": 0})
        e[r.status.lower()] += 1
    for e in out.values():
        scored = e["passed"] + e["failed"]
        e["pass_rate"] = round(e["passed"] / scored, 3) if scored else None
    return out
