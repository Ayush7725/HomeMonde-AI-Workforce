"""Append-only, hash-chained event log. Every state change in the workforce is recorded here first;
the world view, the activity stream and the audit trail are all projections of this log."""
import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .db import TENANT_LOCKS, utcnow
from .models import Event, IdCounter, Tenant

CATEGORY = {
    "DATA_SYNC": "data", "DATA_SOURCE_FAILURE": "data", "DATA_REJECTED": "data", "DATA_STALE": "data",
    "RULE_TRIGGERED": "data",
    "HUMAN_DECISION": "action", "PRE_ACTION_CHECKS": "action", "ACTION_EXECUTED": "action",
    "ACTION_FAILED": "action", "RESULT_VERIFIED": "action",
    "LOGIN": "security", "LOGIN_FAILED": "security", "LOGOUT": "security", "ACCESS_DENIED": "security",
    "ACCESS_GRANTED": "security", "ACCESS_REVOKED": "security",
    "AI_SETTINGS_CHANGED": "security", "AI_KEY_SET": "security", "AI_KEY_REMOVED": "security",
}


def _digest(prev: str, tenant: str, seq: int, ts: str, type_: str, actor: str, work_id, msg: str, payload: dict) -> str:
    body = json.dumps([prev, tenant, seq, ts, type_, actor, work_id, msg, payload], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def lock_tenant(db: Session, tenant_id: str, required: bool = True) -> Tenant | None:
    """Lock the company's row for the rest of this transaction (once). Every writing transaction takes this lock
    before any other row lock, so concurrent API requests and workers always lock in the same order and cannot
    deadlock; the lock also keeps the event sequence and hash chain gap-free."""
    held = db.info.setdefault(TENANT_LOCKS, {})
    if tenant_id not in held:
        with db.no_autoflush:  # take this lock before pending changes lock their own rows
            t = db.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update()).scalar_one_or_none()
        if t is None:
            if required:
                raise LookupError(f"unknown tenant {tenant_id}")
            return None  # the company is being created in this transaction; nobody else can be writing it yet
        held[tenant_id] = t
    return held[tenant_id]


def emit(db: Session, tenant_id: str, type_: str, msg: str, *, actor: str = "system", work_id: str | None = None,
         **payload) -> Event:
    """Append one event to the company's hash chain."""
    t = lock_tenant(db, tenant_id)
    seq = t.event_seq + 1
    ts = utcnow()
    payload = {k: v for k, v in payload.items() if v is not None}
    h = _digest(t.last_hash, tenant_id, seq, ts.isoformat(), type_, actor, work_id, msg, payload)
    ev = Event(tenant_id=tenant_id, seq=seq, ts=ts, type=type_, category=CATEGORY.get(type_, "work"), actor=actor,
               work_id=work_id, msg=msg, payload=payload, prev_hash=t.last_hash, hash=h)
    db.add(ev)
    t.event_seq, t.last_hash = seq, h
    db.flush()
    return ev


def verify_chain(db: Session, tenant_id: str) -> tuple[bool, int | None]:
    """Recompute the chain. Returns (ok, first_bad_seq)."""
    prev = ""
    for ev in db.execute(select(Event).where(Event.tenant_id == tenant_id).order_by(Event.seq)).scalars():
        h = _digest(prev, tenant_id, ev.seq, ev.ts.isoformat(), ev.type, ev.actor, ev.work_id, ev.msg, ev.payload)
        if ev.prev_hash != prev or ev.hash != h:
            return False, ev.seq
        prev = ev.hash
    return True, None


def next_id(db: Session, tenant_id: str, prefix: str) -> str:
    """Human-readable ids like INV-2026-00452, unique per tenant."""
    year = utcnow().year
    key = f"{prefix}-{year}"
    lock_tenant(db, tenant_id)
    row = db.execute(select(IdCounter).where(IdCounter.tenant_id == tenant_id, IdCounter.prefix == key).with_for_update()).scalar_one_or_none()
    if row is None:
        row = IdCounter(tenant_id=tenant_id, prefix=key, value=0)
        db.add(row)
        db.flush()
    db.execute(update(IdCounter).where(IdCounter.tenant_id == tenant_id, IdCounter.prefix == key).values(value=IdCounter.value + 1))
    db.refresh(row)
    return f"{key}-{row.value:05d}"


def to_dict(ev: Event) -> dict:
    return {"seq": ev.seq, "ts": ev.ts.isoformat() + "Z", "type": ev.type, "category": ev.category, "actor": ev.actor,
            "work_id": ev.work_id, "msg": ev.msg, **{k: v for k, v in ev.payload.items() if k not in ("seq", "ts", "type", "msg")}}
