"""Persistent model. Every business row carries tenant_id; queries must always filter by it."""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    config_hash: Mapped[str] = mapped_column(String(64), default="")
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdCounter(Base):
    __tablename__ = "id_counters"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(16), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # owner | manager
    areas: Mapped[list] = mapped_column(JsonType, default=list)  # manager business scope (area ids)
    approval_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notifications_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")


class SourceState(Base):
    __tablename__ = "source_states"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="NEVER")  # OK | FAILED | NEVER
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    # Datasets of a source that synced whose own file could not be used last time; they are stale, the rest are not.
    failed_datasets: Mapped[list | None] = mapped_column(JsonType, nullable=True)


class Record(Base):
    """One normalised row of a tenant dataset. Snapshot datasets keep one row per key per snapshot date (history)."""
    __tablename__ = "records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    dataset: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(300))
    occurred_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    data: Mapped[dict] = mapped_column(JsonType)
    raw: Mapped[dict | None] = mapped_column(JsonType, nullable=True)  # the row exactly as the source sent it
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "dataset", "key", name="uq_record_key"),
        Index("ix_record_window", "tenant_id", "dataset", "occurred_on"),
    )


class WorkItem(Base):
    """Keyed by (tenant_id, id): ids like INV-2026-00001 are numbered per company and never collide across tenants."""
    __tablename__ = "work_items"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    priority: Mapped[str] = mapped_column(String(10), default="MEDIUM")
    employee: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str] = mapped_column(String(500), default="")
    context: Mapped[dict] = mapped_column(JsonType, default=dict)
    origin: Mapped[str] = mapped_column(String(20))  # rule | question | request | schedule | check | data | quality
    rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    next_step: Mapped[str] = mapped_column(String(300), default="")
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    finding: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "parent_id"], ["work_items.tenant_id", "work_items.id"]),
                      Index("ix_work_tenant_status", "tenant_id", "status"),
                      Index("ix_work_tenant_rule", "tenant_id", "rule_id"))


class Evidence(Base):
    """A verified input the AI may cite: a tool result, a deterministic calculation or a precomputed metric."""
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    work_id: Mapped[str] = mapped_column(String(40), index=True)  # root work item
    ref: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))  # tool | calc | metric
    employee: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100))
    args: Mapped[dict] = mapped_column(JsonType, default=dict)
    result: Mapped[dict | list] = mapped_column(JsonType)
    freshness: Mapped[str] = mapped_column(String(16), default="CURRENT")
    source: Mapped[str] = mapped_column(String(200), default="")
    record_ids: Mapped[list] = mapped_column(JsonType, default=list)  # the source records behind it (drill-down), capped
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "work_id", "ref", name="uq_evidence_ref"),
                      ForeignKeyConstraint(["tenant_id", "work_id"], ["work_items.tenant_id", "work_items.id"]))


class Approval(Base):
    __tablename__ = "approvals"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    work_id: Mapped[str] = mapped_column(String(40))
    employee: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JsonType)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="PENDING")  # PENDING | APPROVED | MODIFIED | REJECTED | MORE_INVESTIGATION | EXPIRED
    round: Mapped[int] = mapped_column(Integer, default=1)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    final_params: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)  # handed to a named approver
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "work_id"], ["work_items.tenant_id", "work_items.id"]),
                      Index("ix_approval_tenant_status", "tenant_id", "status"),
                      Index("ix_approval_tenant_work", "tenant_id", "work_id"))


class ActionRecord(Base):
    __tablename__ = "actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    work_id: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(20))  # PENDING | EXECUTED | FAILED | VERIFIED
    external_ref: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "work_id"], ["work_items.tenant_id", "work_items.id"]),
                      ForeignKeyConstraint(["tenant_id", "approval_id"], ["approvals.tenant_id", "approvals.id"]))


class Event(Base):
    """Append-only, hash-chained audit and activity log. The world view and audit trail both read from here."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    type: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(16))  # work | data | action | security
    actor: Mapped[str] = mapped_column(String(100))
    work_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    msg: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "seq", name="uq_event_seq"),
                      Index("ix_event_tenant_type_ts", "tenant_id", "type", "ts"))


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    work_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    employee: Mapped[str] = mapped_column(String(64))
    task: Mapped[str] = mapped_column(String(20), default="investigation")
    tier: Mapped[str] = mapped_column(String(10), default="strong")
    provider: Mapped[str] = mapped_column(String(40), default="anthropic")
    model: Mapped[str] = mapped_column(String(100))
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)  # None when no price is configured
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (Index("ix_usage_tenant_created", "tenant_id", "created_at"),
                      Index("ix_usage_tenant_work", "tenant_id", "work_id"))


class Control(Base):
    """Pause switches (Locks 06 and 17): an AI employee, one action type, or one data feed.
    A paused employee takes no new work and runs no actions; a paused action never executes;
    a paused feed is not synced (its data becomes out of date and rules over it stop firing)."""
    __tablename__ = "controls"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)  # employee | action | source
    target: Mapped[str] = mapped_column(String(64), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FindingReview(Base):
    """Whether a finding or recommendation later turned out right: from a person, or from an automatic outcome check."""
    __tablename__ = "finding_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    work_id: Mapped[str] = mapped_column(String(40))
    employee: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(10))  # human | auto
    verdict: Mapped[str] = mapped_column(String(10))  # CORRECT | PARTLY | WRONG
    note: Mapped[str] = mapped_column(Text, default="")
    reviewer: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "work_id", "source", name="uq_review"),
                      ForeignKeyConstraint(["tenant_id", "work_id"], ["work_items.tenant_id", "work_items.id"]))


class EvalResult(Base):
    """One scenario of an AI-quality run, with the models used, so providers can be compared on the same scenarios."""
    __tablename__ = "eval_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    scenario: Mapped[str] = mapped_column(String(100))
    employee: Mapped[str] = mapped_column(String(64))
    strong_model: Mapped[str] = mapped_column(String(150))
    cheap_model: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(10))  # PASSED | FAILED | SKIPPED
    checks: Mapped[list] = mapped_column(JsonType, default=list)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Report(Base):
    """Daily, weekly or monthly brief built from verified findings; wording may be polished by the cheap model."""
    __tablename__ = "reports"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # daily | weekly | monthly
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    local_date: Mapped[date] = mapped_column(Date)
    content: Mapped[dict] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Notification(Base):
    """A material event worth interrupting someone for (Lock 14). Everything else stays in reports."""
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    severity: Mapped[str] = mapped_column(String(10))  # CRITICAL | HIGH | MEDIUM
    title: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    work_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    employee: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivered: Mapped[str] = mapped_column(String(20), default="in_app")  # pending | in_app | in_app+webhook | webhook_failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (Index("ix_notification_tenant_delivered", "tenant_id", "delivered"),)


class AccessGrant(Base):
    """Temporary extra access to business areas (Lock 09): explicit, time-limited and audited. Expires on its own."""
    __tablename__ = "access_grants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    areas: Mapped[list] = mapped_column(JsonType)
    reason: Mapped[str] = mapped_column(Text)
    granted_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AiRoute(Base):
    """A model choice made by an owner in the panel. Overrides the tenant pack's ai.strong / ai.cheap."""
    __tablename__ = "ai_routes"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    tier: Mapped[str] = mapped_column(String(10), primary_key=True)  # strong | cheap
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(100))
    effort: Mapped[str] = mapped_column(String(10), default="medium")
    price_input: Mapped[float | None] = mapped_column(Float, nullable=True)  # USD per million tokens
    price_output: Mapped[float | None] = mapped_column(Float, nullable=True)
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProviderKey(Base):
    """A model provider API key entered in the panel, encrypted with the deployment's secret key. Never returned."""
    __tablename__ = "provider_keys"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    secret: Mapped[str] = mapped_column(Text)
    hint: Mapped[str] = mapped_column(String(8), default="")  # last 4 characters, to recognise it
    set_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    set_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Heartbeat(Base):
    """Liveness of background processes (one row per worker), so health checks can tell a stalled worker from an idle one."""
    __tablename__ = "heartbeats"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
