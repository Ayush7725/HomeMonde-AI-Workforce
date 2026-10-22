import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC timestamp. All stored datetimes are UTC without tzinfo (portable across Postgres and SQLite)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_engine = None
_Session: sessionmaker | None = None
TENANT_LOCKS = "tenant_locks"  # session.info key: company rows locked in the current transaction


@event.listens_for(Session, "before_flush")
def _tenant_lock_first(session, _ctx, _instances):
    """Writing a company's rows takes that company's lock first (see events.lock_tenant), in a fixed order."""
    if session.info.get("skip_tenant_lock") or session.get_bind().dialect.name != "postgresql":
        return
    tenants = {getattr(o, "tenant_id", None) for o in (*session.new, *session.dirty, *session.deleted)}
    from .events import lock_tenant
    for t in sorted(x for x in tenants if isinstance(x, str)):
        lock_tenant(session, t, required=False)


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _release_tenant_locks(session):
    session.info.pop(TENANT_LOCKS, None)


def _make_engine(url: str):
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _pragmas(conn, _):
            conn.isolation_level = None  # let SQLAlchemy's begin hook control transactions
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        @event.listens_for(eng, "begin")
        def _begin(conn):
            # SQLite has no row locks; take the write lock up front so concurrent writers queue instead of failing.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return eng
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def configure(url: str | None = None) -> None:
    global _engine, _Session
    _engine = _make_engine(url or get_settings().database_url)
    _Session = sessionmaker(_engine, expire_on_commit=False)


def engine():
    if _engine is None:
        configure()
    return _engine


@contextmanager
def session() -> Session:
    if _Session is None:
        configure()
    s = _Session()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def is_postgres() -> bool:
    return engine().dialect.name == "postgresql"


@contextmanager
def exclusive(name: str):
    """Yields True if this process holds the named lock, False if another one does. With several workers on
    Postgres this keeps per-company housekeeping (syncs, rules, schedules) to one worker at a time. SQLite
    deployments run a single worker, so the lock is always granted."""
    if not is_postgres():
        yield True
        return
    key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
    with engine().connect() as conn:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        conn.commit()
        try:
            yield bool(got)
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
