"""Authentication and sessions. Only founders/owners and explicitly authorised managers have accounts.
Passwords use scrypt; sessions are random tokens stored hashed; mutating requests need a CSRF token."""
import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import events
from .db import utcnow
from .models import User, UserSession
from .settings import get_settings

_N, _R, _P = 2 ** 14, 8, 1
MAX_FAILED = 5  # wrong passwords for one account from one address before that address is locked out of it
ACCOUNT_MAX_FAILED = 50  # from any addresses: then the account itself is locked (a spread-out attack)
LOCKOUT = timedelta(minutes=15)
MIN_PASSWORD = 12


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        cand = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(dk)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(cand.hex(), dk)


_DUMMY = None

# Failed sign-ins per client address. Many failures from one address (guessing passwords, or locking other people out on
# purpose) are refused for a while before any account is even checked. Kept in memory: per API process.
IP_MAX_FAILED = 20
IP_WINDOW = timedelta(minutes=15)
_ip_failures: dict[str, list] = {}
_pair_failures: dict[tuple[str, str], list] = {}  # (account, address) -> recent failed attempts


def ip_blocked(ip: str) -> bool:
    cutoff = utcnow() - IP_WINDOW
    recent = [t for t in _ip_failures.get(ip, []) if t > cutoff]
    if recent:
        _ip_failures[ip] = recent
    else:
        _ip_failures.pop(ip, None)
    return len(recent) >= IP_MAX_FAILED


def note_failure(ip: str) -> None:
    if len(_ip_failures) > 50_000 or len(_pair_failures) > 50_000:  # bounded memory under a spray from many addresses
        _ip_failures.clear()
        _pair_failures.clear()
    _ip_failures.setdefault(ip, []).append(utcnow())


def authenticate(db: Session, email: str, password: str, ip: str) -> User | None:
    global _DUMMY
    user = db.execute(select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
    now = utcnow()
    if user is None or not user.active:
        _DUMMY = _DUMMY or hash_password(secrets.token_hex(16))
        verify_password(password, _DUMMY)  # equalise timing for unknown accounts
        return None
    pair = (user.email, ip)
    recent = [t for t in _pair_failures.get(pair, []) if t > now - LOCKOUT]
    if len(recent) >= MAX_FAILED or (user.locked_until and user.locked_until > now):
        # Wrong guesses lock the account only for the address they came from, so nobody can lock the owner out
        # everywhere by guessing on purpose; a spread-out attack still locks the account itself.
        verify_password(password, user.password_hash)  # same work as any other attempt, so timing does not reveal the lock
        events.emit(db, user.tenant_id, "LOGIN_FAILED", f"Login blocked for {user.email} from this address: too many failed attempts",
                    actor=f"user:{user.id}", ip=ip)
        return None
    if not verify_password(password, user.password_hash):
        _pair_failures[pair] = recent + [now]
        user.failed_logins += 1
        if user.failed_logins >= ACCOUNT_MAX_FAILED:
            user.locked_until, user.failed_logins = now + LOCKOUT, 0
        events.emit(db, user.tenant_id, "LOGIN_FAILED", f"Failed login for {user.email}", actor=f"user:{user.id}", ip=ip)
        return None
    _pair_failures.pop(pair, None)
    user.failed_logins, user.locked_until = 0, None
    return user


def start_session(db: Session, user: User, ip: str, agent: str) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    db.execute(delete(UserSession).where(UserSession.expires_at < utcnow()))
    db.add(UserSession(token_hash=_h(token), csrf_token=csrf, user_id=user.id, tenant_id=user.tenant_id,
                       expires_at=utcnow() + timedelta(hours=get_settings().session_hours), ip=ip[:64], user_agent=agent[:300]))
    events.emit(db, user.tenant_id, "LOGIN", f"{user.name} signed in", actor=f"user:{user.id}", ip=ip)
    return token, csrf


def resolve(db: Session, token: str | None) -> tuple[User, UserSession] | None:
    if not token:
        return None
    s = db.execute(select(UserSession).where(UserSession.token_hash == _h(token))).scalar_one_or_none()
    if s is None or s.expires_at < utcnow():
        return None
    u = db.get(User, s.user_id)
    if u is None or not u.active or u.tenant_id != s.tenant_id:
        return None
    return u, s


def end_session(db: Session, token: str) -> None:
    db.execute(delete(UserSession).where(UserSession.token_hash == _h(token)))


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- temporary extra access (Lock 09)

MAX_GRANT_HOURS = 168


class AccessError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def active_grants(db: Session, user: User) -> list:
    """Temporary access a user holds right now (expired or revoked grants are ignored)."""
    from .models import AccessGrant
    return db.execute(select(AccessGrant).where(AccessGrant.user_id == user.id, AccessGrant.revoked_at.is_(None),
                                                AccessGrant.expires_at > utcnow()).order_by(AccessGrant.expires_at)).scalars().all()


def granted_areas(db: Session, user: User) -> set[str]:
    return {a for g in active_grants(db, user) for a in g.areas}


def grant_access(db: Session, cfg, owner: User, user_id: int, areas: list[str], hours: int, reason: str):
    """Owners only. Gives a manager time-limited sight of extra areas. It does not change who may approve what."""
    from .models import AccessGrant
    if owner.role != "owner":
        raise AccessError("only owners can grant access", 403)
    target = db.get(User, user_id)
    if target is None or target.tenant_id != cfg.id or not target.active:
        raise AccessError("unknown person", 404)
    if target.role == "owner":
        raise AccessError("owners already see every area")
    areas = sorted(set(areas))
    if not areas or set(areas) - set(cfg.layout.areas):
        raise AccessError("choose one or more valid areas")
    if not 1 <= hours <= MAX_GRANT_HOURS:
        raise AccessError(f"access can last 1 to {MAX_GRANT_HOURS} hours")
    if len(reason.strip()) < 5:
        raise AccessError("give a reason")
    g = AccessGrant(tenant_id=cfg.id, user_id=target.id, areas=areas, reason=reason.strip()[:500], granted_by=owner.id,
                    expires_at=utcnow() + timedelta(hours=hours))
    db.add(g)
    db.flush()
    events.emit(db, cfg.id, "ACCESS_GRANTED", f"{owner.name} gave {target.name} access to {', '.join(areas)} for {hours} hours · {g.reason}",
                actor=f"user:{owner.id}", user=target.id, areas=areas, hours=hours)
    return g


def revoke_access(db: Session, cfg, owner: User, grant_id: int) -> None:
    from .models import AccessGrant
    if owner.role != "owner":
        raise AccessError("only owners can revoke access", 403)
    g = db.get(AccessGrant, grant_id)
    if g is None or g.tenant_id != cfg.id:
        raise AccessError("unknown grant", 404)
    if g.revoked_at is None:
        g.revoked_at = utcnow()
        target = db.get(User, g.user_id)
        events.emit(db, cfg.id, "ACCESS_REVOKED", f"{owner.name} ended {target.name}'s temporary access to {', '.join(g.areas)}",
                    actor=f"user:{owner.id}", user=g.user_id)
