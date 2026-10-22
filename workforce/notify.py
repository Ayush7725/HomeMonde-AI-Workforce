"""Notifications for material events only (Lock 14): high-priority findings, decisions waiting, failed actions
and broken data sources. The text is the verified wording, optionally reworded by the cheap model.

Sending is two steps so that no model call or webhook ever runs while the caller holds database locks:
`send` records the notification with its verified wording inside the caller's transaction, and the worker's
`deliver_pending` rewords it and posts the webhook afterwards, each in its own short transaction."""
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as dbm
from . import events, wording
from .models import Notification
from .tenancy import TenantConfig, resolve_env

log = logging.getLogger(__name__)
PENDING = "pending"
BATCH = 50


def send(db: Session, cfg: TenantConfig, *, kind: str, severity: str, title: str, verified: str,
         work_id: str | None = None, employee: str | None = None) -> Notification:
    n = Notification(tenant_id=cfg.id, kind=kind, severity=severity, title=title[:300], text=verified, work_id=work_id,
                     employee=employee, delivered=PENDING)
    db.add(n)
    db.flush()
    events.emit(db, cfg.id, "NOTIFICATION", f"Notified · {title}", actor="system", work_id=work_id, emp=employee, severity=severity)
    return n


def deliver_pending(cfg: TenantConfig) -> int:
    """Reword and deliver waiting notifications. Each one is finished in its own transaction, so a slow model or
    webhook delays only that notification. A notification is always visible in the app, reworded or not."""
    with dbm.session() as s:
        ids = s.execute(select(Notification.id).where(Notification.tenant_id == cfg.id, Notification.delivered == PENDING)
                        .order_by(Notification.id).limit(BATCH)).scalars().all()
    for nid in ids:
        with dbm.session() as s:
            n = s.get(Notification, nid)
            if n is None or n.delivered != PENDING:
                continue
            verified = n.text
            s.commit()  # nothing is held while the model rewords the text
            n.text = wording.reword(s, cfg, [{"id": "n", "text": verified}], "notification",
                                    employee=n.employee or "system")["lines"]["n"]
            n.delivered = "in_app"
            if cfg.notify.webhook:
                s.commit()
                n.delivered = "in_app+webhook" if _deliver(cfg, n) else "webhook_failed"
    return len(ids)


def _deliver(cfg: TenantConfig, n: Notification) -> bool:
    body = json.dumps({"tenant": cfg.id, "kind": n.kind, "severity": n.severity, "title": n.title, "text": n.text,
                       "work_id": n.work_id}, sort_keys=True).encode()
    headers = {"Content-Type": "application/json"}
    if cfg.notify.secret:
        headers["X-Workforce-Signature"] = "sha256=" + hmac.new(resolve_env(cfg.notify.secret).encode(), body, hashlib.sha256).hexdigest()
    try:
        req = urllib.request.Request(resolve_env(cfg.notify.webhook), data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310 - URL comes from the tenant pack
            return True
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("notification webhook for %s failed: %s", cfg.id, e)
        return False
