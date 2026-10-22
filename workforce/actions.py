"""Human decision and controlled action workflow.

AI recommends → an authorised human decides → pre-action checks → the action runs once (idempotent) →
the result is verified → everything is recorded. The AI can never approve its own recommendation."""
import hashlib
import hmac
import json
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import events, expr, health, notify, work
from .db import utcnow
from .models import ActionRecord, Approval, User, WorkItem
from .tenancy import ActionConfig, TenantConfig, resolve_env

APPROVAL_TTL = timedelta(days=7)


class DecisionError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def amount_of(action: ActionConfig, params: dict) -> float | None:
    if not action.amount:
        return None
    v = expr.evaluate(action.amount, {k: v for k, v in params.items() if isinstance(v, (int, float))})
    return round(float(v), 2) if isinstance(v, (int, float)) else None


def executor_for(cfg: TenantConfig, action_id: str, fallback: str) -> str:
    """The employee drawn performing the action: the one based in the action's area, else the proposer."""
    area = cfg.actions[action_id].area
    return next((k for k, e in cfg.employees.items() if e.area == area), fallback)


def can_approve(user: User, action: ActionConfig, amount: float | None) -> tuple[bool, str]:
    amt = amount or 0
    for ap in action.approvers:
        if ap.role != user.role:
            continue
        if ap.max_amount is not None and amt > ap.max_amount:
            continue
        if user.role == "manager":
            if action.area not in (user.areas or []):
                continue
            if user.approval_limit is not None and amt > user.approval_limit:
                continue
        return True, ""
    return False, f"{user.name} is not authorised to approve {action.label}" + (f" of {amt:,.0f}" if amount else "")


def propose(db: Session, cfg: TenantConfig, item: WorkItem, rec: dict, round_: int = 1) -> Approval | None:
    action = cfg.actions[rec["action"]]
    amount = amount_of(action, rec["params"])
    executor = executor_for(cfg, rec["action"], item.employee)
    if action.level == 1 and not (health.paused_set(db, cfg.id) & {item.employee, executor}) \
            and rec["action"] not in health.paused_set(db, cfg.id, "action"):
        _execute(db, cfg, item, None, rec["action"], rec["params"], amount)
        return None  # a paused employee's automatic action waits for a person instead
    aid = events.next_id(db, cfg.id, "APR")
    ap = Approval(id=aid, tenant_id=cfg.id, work_id=item.id, employee=item.employee, action=rec["action"], params=rec["params"],
                  amount=amount, title=rec["summary"] or action.label, rationale=rec["rationale"], round=round_)
    db.add(ap)
    db.flush()
    who = " or ".join(sorted({a.role for a in action.approvers}))
    events.emit(db, cfg.id, "APPROVAL_REQUESTED",
                f"Approval {aid} requested · {ap.title}" + (f" · {cfg.currency} {amount:,.0f}" if amount else "") + f" → {who}",
                actor=item.employee, work_id=item.id, emp=item.employee, approval=aid, area="approvals",
                amount=amount, level=action.level)
    work.transition(db, cfg, item, "NEEDS_HUMAN_REVIEW", "Wait for a human decision")
    notify.send(db, cfg, kind="decision", severity="HIGH", title=f"Decision waiting · {ap.title}",
                verified=f"Decision waiting: {ap.title}" + (f" ({cfg.currency} {amount:,.0f})." if amount else "."),
                work_id=item.id, employee=item.employee)
    return ap


def _check_params(action: ActionConfig, params: dict) -> dict:
    clean = {}
    for name, ps in action.params.items():
        v = params.get(name)
        if v in (None, ""):
            if ps.required:
                raise DecisionError(f"{name} is required")
            continue
        try:
            v = int(float(v)) if ps.type == "integer" else float(v) if ps.type == "number" else str(v)[:300]
        except (TypeError, ValueError):
            raise DecisionError(f"{name} must be a {ps.type}") from None
        if isinstance(v, (int, float)) and ((ps.minimum is not None and v < ps.minimum) or (ps.maximum is not None and v > ps.maximum)):
            raise DecisionError(f"{name} is out of range")
        if ps.enum and v not in ps.enum:
            raise DecisionError(f"{name} must be one of {ps.enum}")
        clean[name] = v
    return clean


def decide(db: Session, cfg: TenantConfig, approval_id: str, user: User, decision: str, params: dict | None = None,
           reason: str = "") -> Approval:
    events.lock_tenant(db, cfg.id)  # lock order: the company first, then the approval
    ap = db.execute(select(Approval).where(Approval.id == approval_id, Approval.tenant_id == cfg.id).with_for_update()).scalar_one_or_none()
    if ap is None:
        raise DecisionError("approval not found", 404)
    if ap.status != "PENDING":
        raise DecisionError(f"approval is already {ap.status.lower()}", 409)
    item = db.get(WorkItem, (cfg.id, ap.work_id))
    action = cfg.actions[ap.action]
    reason = (reason or "").strip()[:1000]
    if utcnow() - ap.requested_at > APPROVAL_TTL:
        ap.status = "EXPIRED"
        work.transition(db, cfg, item, "COMPLETED", "Approval expired without a decision", outcome="EXPIRED")
        raise DecisionError("approval expired; the evidence is too old to act on", 409)

    if decision in ("APPROVED", "MODIFIED"):  # refuse before recording anything if the action cannot run
        paused = health.paused_set(db, cfg.id) & {item.employee, executor_for(cfg, ap.action, item.employee)}
        if paused:
            raise DecisionError(f"{', '.join(sorted(paused))} is paused; resume it before approving", 409)
        if ap.action in health.paused_set(db, cfg.id, "action"):
            raise DecisionError(f"{action.label} actions are paused; an owner must resume them first", 409)
    final = dict(ap.params)
    if decision == "MODIFIED":
        changed = [k for k, v in (params or {}).items() if k in action.params and action.params[k].locked and str(v) != str(ap.params.get(k))]
        if changed:
            raise DecisionError(f"{', '.join(changed)} cannot be changed when approving; reject and ask for a new proposal instead", 400)
        final = _check_params(action, {**ap.params, **(params or {})})
        if final == ap.params:
            decision = "APPROVED"
    amount = amount_of(action, final)
    if decision in ("APPROVED", "MODIFIED") and action.handler == "email" and not _valid_email(final.get(action.email.to_param)):
        raise DecisionError(f"add the recipient's email address ({action.email.to_param}) before approving", 400)
    if decision in ("APPROVED", "MODIFIED"):
        ok, why = can_approve(user, action, amount)
        if not ok:
            events.emit(db, cfg.id, "ACCESS_DENIED", f"Approval {ap.id} denied for {user.email}: {why}", actor=f"user:{user.id}", work_id=item.id)
            raise DecisionError(why, 403)
    elif decision not in ("REJECTED", "MORE_INVESTIGATION"):
        raise DecisionError("unknown decision")

    ap.status, ap.decided_by, ap.decided_at, ap.reason, ap.final_params = decision, user.id, utcnow(), reason, final
    label = {"APPROVED": "approved", "MODIFIED": "approved with changes", "REJECTED": "rejected",
             "MORE_INVESTIGATION": "asked for more investigation on"}[decision]
    events.emit(db, cfg.id, "HUMAN_DECISION", f"{user.name} {label} {ap.id}" + (f" · {reason}" if reason else ""),
                actor=f"user:{user.id}", work_id=item.id, emp=item.employee, approval=ap.id, decision=decision,
                by=user.name, amount=amount, params=final if decision == "MODIFIED" else None)

    if decision == "REJECTED":
        work.transition(db, cfg, item, "COMPLETED", "Closed · no action taken", outcome="REJECTED")
    elif decision == "MORE_INVESTIGATION":
        item.context = {**item.context, "human_request": reason or "Investigate further before deciding", "previous_approval": ap.id}
        item.attempts, item.next_attempt_at = 0, None
        work.transition(db, cfg, item, "QUEUED", "Re-investigate with the founder's question")
    elif action.level == 3:
        work.transition(db, cfg, item, "COMPLETED", "Human performs this action in the source system", outcome="HUMAN_ACTION")
    else:
        _execute(db, cfg, item, ap, ap.action, final, amount, user)
    return ap


def _execute(db: Session, cfg: TenantConfig, item: WorkItem, ap: Approval | None, action_id: str, params: dict,
             amount: float | None, user: User | None = None) -> ActionRecord:
    action = cfg.actions[action_id]
    key = f"{cfg.id}:{ap.id if ap else item.id}:{action_id}"
    checks = []
    if user:
        checks.append(f"{user.name} is authorised for {action.label}" + (f" ({cfg.currency} {amount:,.0f})" if amount else ""))
    if ap:
        checks.append(f"Approval {ap.id} is valid and recorded")
        checks.append("Parameters match the recorded decision")
    if db.execute(select(ActionRecord.id).where(ActionRecord.idempotency_key == key, ActionRecord.status != "FAILED")).first():
        raise DecisionError("this action has already been executed", 409)
    checks.append("No earlier execution of this action exists")
    executor = executor_for(cfg, action_id, item.employee)
    try:
        health.ensure_running(db, cfg.id, item.employee, executor)
    except health.EmployeePaused as e:
        raise DecisionError(f"{e} is paused; resume it before any action runs", 409) from None
    if action_id in health.paused_set(db, cfg.id, "action"):
        raise DecisionError(f"{action.label} actions are paused; an owner must resume them first", 409)
    checks.append("The AI employees involved are not paused")
    events.emit(db, cfg.id, "PRE_ACTION_CHECKS", f"Pre-action checks passed · {len(checks)} of {len(checks)}",
                actor=executor, work_id=item.id, emp=item.employee, checks=checks)
    work.transition(db, cfg, item, "RUNNING", f"Execute {action.label}")

    rec = db.execute(select(ActionRecord).where(ActionRecord.idempotency_key == key)).scalar_one_or_none()
    if rec is None:
        rec = ActionRecord(tenant_id=cfg.id, idempotency_key=key, approval_id=ap.id if ap else None, work_id=item.id,
                           action=action_id, params=params, status="PENDING")
        db.add(rec)
        db.flush()
    rec.status, rec.created_at = "PENDING", utcnow()
    if action.handler != "record":
        db.commit()  # the decision and checks are saved before the target system is called; no locks are held meanwhile
    try:
        ref, verified, detail = HANDLERS[action.handler](cfg, action, action_id, params, rec, ap, item)
    except Exception as e:  # the failure is surfaced, never reported as success
        _failed(db, cfg, item, rec, action, executor, str(e))
        return rec
    rec.status, rec.external_ref, rec.detail = ("VERIFIED" if verified else "EXECUTED"), ref, detail
    events.emit(db, cfg.id, "ACTION_EXECUTED", f"{cfg.employees[executor].name} ran {action_id} → {ref}",
                actor=executor, work_id=item.id, emp=executor, action=action_id, ref=ref)
    if verified:
        events.emit(db, cfg.id, "RESULT_VERIFIED", f"Result verified · {detail}", actor=executor, work_id=item.id, emp=executor, ref=ref)
    work.transition(db, cfg, item, "COMPLETED", "Closed · outcome tracked for evaluation", outcome="ACTION_TAKEN")
    return rec


def _failed(db: Session, cfg: TenantConfig, item: WorkItem, rec: ActionRecord, action: ActionConfig, executor: str, why: str) -> None:
    rec.status, rec.detail = "FAILED", why[:500]
    events.emit(db, cfg.id, "ACTION_FAILED", f"{action.label} failed: {rec.detail}. Retry required.",
                actor=executor, work_id=item.id, emp=executor, action=rec.action, approval=rec.approval_id)
    work.transition(db, cfg, item, "NEEDS_HUMAN_REVIEW", "Action failed · retry required", outcome="ACTION_FAILED")
    notify.send(db, cfg, kind="action_failed", severity="HIGH", title=f"Action failed · {action.label}",
                verified=f"{action.label} failed and needs a retry: {rec.detail}", work_id=item.id, employee=executor)


STUCK_AFTER = timedelta(minutes=10)


def fail_unconfirmed(db: Session, cfg: TenantConfig) -> int:
    """An action whose run was cut off (crash or restart while waiting for the target system) is shown as failed so a
    person can retry it. Retrying is safe: the target receives the same idempotency key and can ignore a repeat."""
    stuck = db.execute(select(ActionRecord).where(ActionRecord.tenant_id == cfg.id, ActionRecord.status == "PENDING",
                                                  ActionRecord.created_at < utcnow() - STUCK_AFTER)).scalars().all()
    for rec in stuck:
        item = db.get(WorkItem, (cfg.id, rec.work_id))
        action = cfg.actions.get(rec.action)
        if item is None or action is None:
            rec.status, rec.detail = "FAILED", "no confirmation from the target system"
            continue
        _failed(db, cfg, item, rec, action, executor_for(cfg, rec.action, item.employee),
                "no confirmation from the target system; check it there before retrying")
    return len(stuck)


def retry_action(db: Session, cfg: TenantConfig, approval_id: str, user: User) -> ActionRecord:
    ap = db.execute(select(Approval).where(Approval.id == approval_id, Approval.tenant_id == cfg.id)).scalar_one_or_none()
    if ap is None or ap.status not in ("APPROVED", "MODIFIED"):
        raise DecisionError("nothing to retry", 409)
    rec = db.execute(select(ActionRecord).where(ActionRecord.tenant_id == cfg.id, ActionRecord.approval_id == ap.id)).scalar_one_or_none()
    if rec is None or rec.status != "FAILED":
        raise DecisionError("the action did not fail", 409)
    action = cfg.actions[ap.action]
    ok, why = can_approve(user, action, ap.amount if ap.final_params is None else amount_of(action, ap.final_params))
    if not ok:
        raise DecisionError(why, 403)
    return _execute(db, cfg, db.get(WorkItem, (cfg.id, ap.work_id)), ap, ap.action, ap.final_params or ap.params,
                    amount_of(action, ap.final_params or ap.params), user)


def _record(cfg, action, action_id, params, rec, ap, item):
    """Internal register (e.g. purchase requests reviewed by the team in the source system)."""
    rec.external_ref = f"{action_id.upper().replace('_', '-')}-{rec.id:05d}"
    return rec.external_ref, True, f"{rec.external_ref} recorded in the action register"


def _webhook(cfg, action, action_id, params, rec, ap, item):
    body = json.dumps({"tenant": cfg.id, "action": action_id, "params": params, "approval_id": ap.id if ap else None,
                       "work_id": item.id, "idempotency_key": rec.idempotency_key}, sort_keys=True).encode()
    headers = {"Content-Type": "application/json", "Idempotency-Key": rec.idempotency_key}
    if action.secret:
        headers["X-Workforce-Signature"] = "sha256=" + hmac.new(resolve_env(action.secret).encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(resolve_env(action.url), data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - URL comes from the tenant pack
            payload = resp.read(65536)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"target system returned HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"target system unreachable ({e.reason})") from None
    try:
        data = json.loads(payload or b"{}")
    except ValueError:
        data = {}
    ref = str(data.get("reference") or "")[:200]
    return ref or f"accepted-{rec.id}", bool(ref), (f"{ref} confirmed by the target system" if ref else "accepted, no reference returned")


def expire_stale(db: Session, cfg: TenantConfig) -> int:
    """Close decisions nobody made within APPROVAL_TTL, so old evidence is never acted on and queues stay honest."""
    old = db.execute(select(Approval).where(Approval.tenant_id == cfg.id, Approval.status == "PENDING",
                                            Approval.requested_at < utcnow() - APPROVAL_TTL)).scalars().all()
    for ap in old:
        ap.status = "EXPIRED"
        events.emit(db, cfg.id, "APPROVAL_EXPIRED", f"Approval {ap.id} expired without a decision", work_id=ap.work_id,
                    emp=ap.employee, approval=ap.id)
        item = db.get(WorkItem, (cfg.id, ap.work_id))
        if item is not None and item.status not in work.TERMINAL:
            work.transition(db, cfg, item, "COMPLETED", "Approval expired without a decision", outcome="EXPIRED")
    return len(old)


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(v) -> bool:
    return isinstance(v, str) and bool(EMAIL.match(v.strip()))


def _email(cfg, action, action_id, params, rec, ap, item):
    """Send the approved text exactly as approved. The Message-ID is derived from the idempotency key, so a retried
    send carries the same ID and mail systems and recipients can recognise the repeat."""
    e = action.email
    url = urllib.parse.urlsplit(resolve_env(e.smtp_url))
    if url.scheme not in ("smtp", "smtps") or not url.hostname:
        raise RuntimeError("mail server address must look like smtp://user:password@host:587")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = resolve_env(e.sender), str(params[e.to_param]).strip(), str(params[e.subject_param])
    if e.reply_to:
        msg["Reply-To"] = resolve_env(e.reply_to)
    msg["Message-ID"] = f"<{hashlib.sha256(rec.idempotency_key.encode()).hexdigest()[:32]}@workforce>"
    msg.set_content(str(params[e.body_param]))
    try:
        if url.scheme == "smtps":
            server = smtplib.SMTP_SSL(url.hostname, url.port or 465, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(url.hostname, url.port or 587, timeout=20)
        with server:
            if url.scheme == "smtp":
                server.starttls(context=ssl.create_default_context())
            if url.username:
                server.login(urllib.parse.unquote(url.username), urllib.parse.unquote(url.password or ""))
            refused = server.send_message(msg)
    except (smtplib.SMTPException, OSError) as err:
        raise RuntimeError(f"mail server did not accept the message ({type(err).__name__})") from None
    if refused:
        raise RuntimeError("the mail server refused the recipient")
    return msg["Message-ID"], True, f"sent to {msg['To']} as {msg['Message-ID']}"


HANDLERS = {"record": _record, "webhook": _webhook, "email": _email}


def approvers_for(db: Session, cfg: TenantConfig, ap: Approval) -> list[User]:
    """Active people who are allowed to decide this approval (role, area and amount limits)."""
    action = cfg.actions[ap.action]
    users = db.execute(select(User).where(User.tenant_id == cfg.id, User.active)).scalars().all()
    return [u for u in users if can_approve(u, action, ap.amount)[0]]


def assign(db: Session, cfg: TenantConfig, approval_id: str, user: User, target_id: int, note: str = "") -> Approval:
    """Hand a waiting decision to a named person who is allowed to make it (Lock 15: ASSIGN)."""
    events.lock_tenant(db, cfg.id)
    ap = db.execute(select(Approval).where(Approval.id == approval_id, Approval.tenant_id == cfg.id).with_for_update()).scalar_one_or_none()
    if ap is None:
        raise DecisionError("approval not found", 404)
    if ap.status != "PENDING":
        raise DecisionError(f"approval is already {ap.status.lower()}", 409)
    if target_id == user.id:
        raise DecisionError("choose someone other than yourself")
    target = next((u for u in approvers_for(db, cfg, ap) if u.id == target_id), None)
    if target is None:
        raise DecisionError("that person is not allowed to make this decision", 403)
    ap.assigned_to = target.id
    note = note.strip()[:500]
    events.emit(db, cfg.id, "APPROVAL_ASSIGNED", f"{user.name} handed {ap.id} to {target.name}" + (f" · {note}" if note else ""),
                actor=f"user:{user.id}", work_id=ap.work_id, emp=ap.employee, approval=ap.id, by=user.name, to_user=target.name)
    notify.send(db, cfg, kind="decision", severity="HIGH", title=f"Decision handed to {target.name} · {ap.title}",
                verified=f"{user.name} asked {target.name} to decide: {ap.title}." + (f" Note: {note}" if note else ""),
                work_id=ap.work_id, employee=ap.employee)
    return ap
