"""Work queue: every piece of AI work is a durable, prioritised, leased work item with an audited lifecycle."""
from datetime import timedelta

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from . import events
from .db import is_postgres, utcnow
from .models import Control, WorkItem
from .tenancy import TenantConfig

TERMINAL = {"COMPLETED", "FAILED"}
ACTIVE = {"RUNNING", "INVESTIGATING", "VALIDATING"}
PRIORITY_ORDER = case({"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}, value=WorkItem.priority, else_=4)
MAX_ATTEMPTS = 3


def create(db: Session, cfg: TenantConfig, *, employee: str, type_: str, title: str, origin: str, priority: str = "MEDIUM",
           subject: str = "", context: dict | None = None, rule_id: str | None = None, dedupe_key: str | None = None,
           parent_id: str | None = None, created_by: int | None = None, next_step: str = "", status: str = "QUEUED",
           msg: str | None = None) -> WorkItem:
    emp = cfg.employees[employee]
    wid = events.next_id(db, cfg.id, emp.prefix)
    item = WorkItem(id=wid, tenant_id=cfg.id, type=type_, priority=priority, employee=employee, title=title[:300],
                    subject=subject[:500], context=context or {}, origin=origin, rule_id=rule_id, dedupe_key=dedupe_key,
                    parent_id=parent_id, created_by=created_by, next_step=next_step, status=status)
    db.add(item)
    db.flush()
    events.emit(db, cfg.id, "WORK_ITEM_CREATED",
                msg or f"Work item {wid} created · {type_} · {priority} → {emp.name} queue",
                actor=employee, work_id=wid, emp=employee, wtype=type_, priority=priority, parent=parent_id, title=item.title)
    return item


def transition(db: Session, cfg: TenantConfig, item: WorkItem, status: str, next_step: str | None = None,
               outcome: str | None = None, error: str | None = None) -> None:
    item.status = status
    if next_step is not None:
        item.next_step = next_step[:300]
    if outcome is not None:
        item.outcome = outcome
    if error is not None:
        item.error = error[:2000]
    if status in TERMINAL:
        item.completed_at = utcnow()
    if status not in ACTIVE:  # only a running item holds a lease; a re-queued item must be claimable at once
        item.lease_until = None
    events.emit(db, cfg.id, "STATE_CHANGED", f"{item.id} → {status}" + (f" · next: {next_step}" if next_step else ""),
                actor=item.employee, work_id=item.id, emp=item.employee, status=status, parent=item.parent_id,
                next=next_step, outcome=outcome)


def claim(db: Session, tenant_ids: list[str], lease_seconds: int) -> WorkItem | None:
    """Lease the next runnable top-level item. Safe with several workers on Postgres (SKIP LOCKED).
    The item stays QUEUED until its runner moves it on, so an unexpired lease is what keeps it from being
    claimed twice; a lease that ran out (the worker died before starting) makes it claimable again."""
    now = utcnow()
    paused = select(Control.target).where(Control.tenant_id == WorkItem.tenant_id, Control.kind == "employee", Control.paused)
    q = (select(WorkItem)
         .where(WorkItem.tenant_id.in_(tenant_ids), WorkItem.parent_id.is_(None),
                WorkItem.status.in_(["QUEUED", "NEEDS_RETRY"]), WorkItem.employee.notin_(paused.scalar_subquery()),
                or_(WorkItem.next_attempt_at.is_(None), WorkItem.next_attempt_at <= now),
                or_(WorkItem.lease_until.is_(None), WorkItem.lease_until < now))
         .order_by(PRIORITY_ORDER, WorkItem.created_at).limit(1))
    q = q.with_for_update(skip_locked=True) if is_postgres() else q
    item = db.execute(q).scalar_one_or_none()
    if item:
        item.lease_until = now + timedelta(seconds=lease_seconds)
        item.attempts += 1
        db.info["skip_tenant_lock"] = True  # a lease update writes no events and must not wait behind the company lock
        try:
            db.flush()
        finally:
            db.info.pop("skip_tenant_lock", None)
    return item


def recover_expired(db: Session, cfgs: dict[str, TenantConfig]) -> int:
    """Items whose worker died mid-run go back to the queue (or fail after MAX_ATTEMPTS). Nothing is silently lost."""
    now = utcnow()
    stuck = db.execute(select(WorkItem).where(WorkItem.tenant_id.in_(list(cfgs)), WorkItem.status.in_(list(ACTIVE)),
                                              WorkItem.parent_id.is_(None), WorkItem.lease_until < now)).scalars().all()
    for item in stuck:
        retry(db, cfgs[item.tenant_id], item, "worker lease expired")
    return len(stuck)


def retry(db: Session, cfg: TenantConfig, item: WorkItem, reason: str, wait: float | None = None) -> None:
    """Schedule another attempt. `wait` is a pause the AI provider asked for (a rate limit): the item waits exactly that
    long and the attempt is not counted, since nothing went wrong with the work itself."""
    if wait is not None:
        item.attempts = max(0, item.attempts - 1)
        item.next_attempt_at = utcnow() + timedelta(seconds=max(wait, 5))
        item.lease_until = None
        transition(db, cfg, item, "NEEDS_RETRY", f"Waiting {max(wait, 5):.0f}s: the AI provider asked to slow down", error=reason)
        return
    if item.attempts >= MAX_ATTEMPTS:
        transition(db, cfg, item, "FAILED", "Escalated: failed after retries", outcome="INVESTIGATION_FAILED", error=reason)
        return
    item.next_attempt_at = utcnow() + timedelta(seconds=60 * 2 ** item.attempts)
    item.lease_until = None
    transition(db, cfg, item, "NEEDS_RETRY", f"Retry {item.attempts + 1} of {MAX_ATTEMPTS} scheduled", error=reason)


def item_groups(context: dict) -> list:
    """The rule groups (e.g. SKUs) a task covers: one for a single alert, several for a batched one."""
    if "groups" in context:
        return list(context["groups"])
    return [context["group"]] if "group" in context else []


def groups_covered(db: Session, tenant_id: str, rule_id: str, cooldown_hours: float) -> set[str]:
    """Groups of a rule that already have an open task, or one created within the cooldown. One query per rule."""
    since = utcnow() - timedelta(hours=cooldown_hours)
    rows = db.execute(select(WorkItem.context).where(
        WorkItem.tenant_id == tenant_id, WorkItem.rule_id == rule_id, WorkItem.parent_id.is_(None),
        or_(WorkItem.status.notin_(list(TERMINAL)), WorkItem.created_at >= since))).scalars()
    return {str(g) for ctx in rows for g in item_groups(ctx or {})}


def open_or_recent(db: Session, tenant_id: str, dedupe_key: str, cooldown_hours: float) -> bool:
    since = utcnow() - timedelta(hours=cooldown_hours)
    return db.execute(select(WorkItem.id).where(
        WorkItem.tenant_id == tenant_id, WorkItem.dedupe_key == dedupe_key,
        or_(WorkItem.status.notin_(list(TERMINAL)), WorkItem.created_at >= since)).limit(1)).first() is not None


class WorkError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def request_stop(db: Session, cfg: TenantConfig, item: WorkItem, user) -> str:
    """Stop a task. Waiting tasks stop now; a running task stops at its next step (nothing half-done is kept as a result)."""
    from .models import Approval
    if item.parent_id:
        raise WorkError("stop the main task instead of a help request")
    if item.status in TERMINAL:
        raise WorkError("this task has already finished", 409)
    if db.execute(select(Approval.id).where(Approval.tenant_id == cfg.id, Approval.work_id == item.id, Approval.status == "PENDING")).first():
        raise WorkError("this task is waiting for a decision; say no to it instead", 409)
    events.emit(db, cfg.id, "WORK_STOP_REQUESTED", f"{user.name} stopped {item.id}", actor=f"user:{user.id}", work_id=item.id,
                emp=item.employee, by=user.name)
    if item.status in ACTIVE:
        item.stop_requested = True
        return "stopping"
    transition(db, cfg, item, "COMPLETED", f"Stopped by {user.name}", outcome="STOPPED")
    return "stopped"


def resolve_review(db: Session, cfg: TenantConfig, item: WorkItem, user, action: str, note: str = "") -> None:
    """A person closes a task that was waiting for human review, or sends it back for another investigation."""
    from .models import Approval
    if item.status != "NEEDS_HUMAN_REVIEW":
        raise WorkError("this task is not waiting for a review", 409)
    if db.execute(select(Approval.id).where(Approval.tenant_id == cfg.id, Approval.work_id == item.id, Approval.status == "PENDING")).first():
        raise WorkError("this task is waiting for a decision on a suggestion; decide there", 409)
    if action == "reinvestigate" and item.type == "RULE_REVIEW":
        raise WorkError("a rule review is settled by an owner; close it once decided", 409)
    if action == "reinvestigate" and item.type == "DATA_ISSUE":
        raise WorkError("a data problem is fixed at its source; it closes on its own once the data comes through", 409)
    note = note.strip()[:1000]
    events.emit(db, cfg.id, "REVIEW_RESOLVED", f"{user.name} {'closed' if action == 'close' else 'reopened'} {item.id}" + (f" · {note}" if note else ""),
                actor=f"user:{user.id}", work_id=item.id, emp=item.employee, by=user.name, decision=action)
    if action == "close":
        transition(db, cfg, item, "COMPLETED", f"Reviewed and closed by {user.name}", outcome="REVIEWED")
    elif action == "reinvestigate":
        item.context = {**item.context, "human_request": note or "Investigate again"}
        item.attempts, item.next_attempt_at = 0, None
        transition(db, cfg, item, "QUEUED", "Investigate again at a person's request")
    else:
        raise WorkError("action must be close or reinvestigate")
