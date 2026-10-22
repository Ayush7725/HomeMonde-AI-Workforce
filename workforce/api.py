"""HTTP API and the management web app. Every request is authenticated, tenant-scoped and, for managers,
limited to their authorised business areas."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from . import actions, ai_settings, data, db, events, expr, health, home, quality, reports, runtime, security, tenancy, work
from .db import utcnow
from .llm import LLM, route_label
from .models import ActionRecord, Approval, Event, Evidence, EvalResult, FindingReview, LlmUsage, Notification, Record, Report, SourceState, User, UserSession, WorkItem
from .settings import get_settings

log = logging.getLogger("workforce.api")
WEB = Path(__file__).parent / "web"
COOKIE = "wf_session"
QUESTIONS_PER_HOUR = 30


class Ctx:
    def __init__(self, user: User, sess: UserSession, cfg: tenancy.TenantConfig, extra_areas: set[str] = frozenset()):
        self.user, self.sess, self.cfg = user, sess, cfg
        self.extra_areas = set(extra_areas)  # temporary access, already filtered for expiry

    @property
    def is_owner(self) -> bool:
        return self.user.role == "owner"

    def areas(self) -> set[str] | None:
        """Business areas this user covers; None means all (owners)."""
        return None if self.is_owner else set(self.user.areas or []) | self.extra_areas

    def employees(self) -> set[str]:
        """AI employees whose work this user may see."""
        if self.is_owner:
            return set(self.cfg.employees)
        return {k for k, e in self.cfg.employees.items() if e.area in self.areas()}

    def can_see_approval(self, ap: Approval) -> bool:
        return home.approval_visible(self.cfg, ap, self.employees(), self.areas())

    def can_see_work(self, s, item: WorkItem) -> bool:
        """A task is visible through its employee, because this user asked it, or through a decision on it this user may
        see. What its colleagues found in areas this user does not cover is withheld separately (see _withhold)."""
        if item.employee in self.employees():
            return True
        root = item
        while root.parent_id:
            root = s.get(WorkItem, (self.cfg.id, root.parent_id))
        if root.created_by == self.user.id:
            return True
        aps = s.execute(select(Approval).where(Approval.tenant_id == self.cfg.id, Approval.work_id == root.id)).scalars()
        return any(self.can_see_approval(a) for a in aps)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app):
        with db.session() as s:
            tenancy.register_all(s)  # refuse to start on an invalid tenant pack
        yield

    app = FastAPI(title="AI Workforce", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(OperationalError)
    async def _database_unavailable(_request: Request, exc: OperationalError):
        """A database restart or network blip: a clear, retryable answer instead of a server error and a traceback."""
        log.warning("database unavailable: %s", str(exc.orig or exc).splitlines()[0][:200])
        return JSONResponse({"detail": "The system is briefly unavailable. Please try again in a moment."},
                            status_code=503, headers={"Retry-After": "5"})
    if settings.allowed_hosts != "*":
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in settings.allowed_hosts.split(",")])

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # the app's own files: browsers re-check them (a cheap 304 when unchanged) so an update is never half-applied
            resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if settings.secure_cookies:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def current(request: Request) -> Ctx:
        token = request.cookies.get(COOKIE)
        with db.session() as s:
            found = security.resolve(s, token)
            if not found:
                raise HTTPException(401, "sign in required")
            user, sess = found
            extra = security.granted_areas(s, user)
            s.expunge(user)
            s.expunge(sess)
        if request.method not in ("GET", "HEAD") and request.headers.get("X-CSRF-Token") != sess.csrf_token:
            raise HTTPException(403, "missing or invalid CSRF token")
        return Ctx(user, sess, tenancy.get(user.tenant_id), extra)

    # ------------------------------------------------------------------ auth
    class Login(BaseModel):
        email: str = Field(max_length=254)
        password: str = Field(max_length=200)

    @app.post("/api/auth/login")
    def login(body: Login, request: Request):
        ip = request.client.host if request.client else ""
        if security.ip_blocked(ip):
            return JSONResponse({"detail": "Too many failed sign-ins from this address. Try again in 15 minutes."}, status_code=429)
        with db.session() as s:
            user = security.authenticate(s, body.email, body.password, ip)
            if user is None:
                ok = None
            else:
                token, csrf = security.start_session(s, user, ip, request.headers.get("user-agent", ""))
                ok = (token, csrf, _user_json(user))
        if ok is None:
            security.note_failure(ip)
            return JSONResponse({"detail": "Email or password is incorrect, or the account is locked."}, status_code=401)
        token, csrf, uj = ok
        resp = JSONResponse({"user": uj, "csrf": csrf})
        resp.set_cookie(COOKIE, token, httponly=True, secure=settings.secure_cookies, samesite="strict",
                        max_age=settings.session_hours * 3600, path="/")
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request, c: Ctx = Depends(current)):
        with db.session() as s:
            security.end_session(s, request.cookies.get(COOKIE, ""))
            events.emit(s, c.cfg.id, "LOGOUT", f"{c.user.name} signed out", actor=f"user:{c.user.id}")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/me")
    def me(c: Ctx = Depends(current)):
        return {"user": _user_json(c.user), "csrf": c.sess.csrf_token}

    # ------------------------------------------------------------------ world + state
    @app.get("/api/bootstrap")
    def bootstrap(c: Ctx = Depends(current)):
        cfg = c.cfg
        visible = c.employees()
        with db.session() as s:
            sources = _sources(cfg, s)
            temporary = [{"areas": g.areas, "expires_at": _iso(g.expires_at), "reason": g.reason} for g in security.active_grants(s, c.user)]
            ai = ai_settings.effective(s, cfg).ai
        return {
            "tenant": {"id": cfg.id, "name": cfg.name, "currency": cfg.currency, "timezone": cfg.timezone},
            "user": {**_user_json(c.user), "temporary_access": temporary}, "csrf": c.sess.csrf_token,
            "employees": {k: {"name": e.name, "title": e.title, "role": e.role, "area": e.area, "color": e.color,
                              "answers_questions": e.answers_questions, "sensitive": e.sensitive}
                          for k, e in cfg.employees.items() if k in visible or e.answers_questions},
            "tools": {k: {"area": t.area, "spot": t.spot, "label": t.label, "description": t.description} for k, t in cfg.tools.items()},
            "datasets": {k: {"label": d.label, "source": d.source} for k, d in cfg.datasets.items()},
            "area_data": _area_data(cfg),
            "actions": {k: {"label": a.label, "area": a.area, "level": a.level,
                            "params": {n: p.model_dump() for n, p in a.params.items()}} for k, a in cfg.actions.items()},
            "layout": cfg.layout.model_dump(),
            "sources": sources,
            "ai": {"strong": route_label(ai.strong), "cheap": route_label(ai.cheap),
                   "routing": {t: ai.tier_for(t) for t in tenancy.TASKS}} if c.is_owner else None,
        }

    @app.get("/api/state")
    def state(c: Ctx = Depends(current)):
        cfg, visible = c.cfg, c.employees()
        with db.session() as s:
            active = s.execute(select(WorkItem).where(WorkItem.tenant_id == cfg.id, WorkItem.status.notin_(list(work.TERMINAL)),
                                                      WorkItem.employee.in_(visible))
                               .order_by(work.PRIORITY_ORDER, WorkItem.created_at.desc()).limit(200)).scalars().all()
            recent = s.execute(select(WorkItem).where(WorkItem.tenant_id == cfg.id, WorkItem.status.in_(list(work.TERMINAL)),
                                                      WorkItem.employee.in_(visible), WorkItem.parent_id.is_(None))
                               .order_by(WorkItem.completed_at.desc()).limit(30)).scalars().all()
            approvals = s.execute(select(Approval).where(Approval.tenant_id == cfg.id, Approval.status == "PENDING")).scalars().all()
            approvals = [a for a in approvals if c.can_see_approval(a)]
            emp_state = {}
            for k in cfg.employees:
                mine = [w for w in active if w.employee == k]
                cur = next((w for w in mine if w.status in work.ACTIVE), None) or next(iter(mine), None)
                last = next((w for w in recent if w.employee == k), None)
                status = "IDLE"
                if any(w.status == "NEEDS_HUMAN_REVIEW" for w in mine):
                    status = "WAITING_APPROVAL"
                elif any(w.status in work.ACTIVE for w in mine):
                    status = "WORKING"
                elif mine:
                    status = "QUEUED"
                emp_state[k] = {"status": status, "current": cur.id if cur else None, "queue": len(mine),
                                "last": last.id if last else None}
            paused = health.paused_set(s, cfg.id)
            for k, v in emp_state.items():
                v["paused"] = k in paused
            usage = LLM.load(s, cfg).tokens_today(s) if c.is_owner else None
            seq = s.execute(select(func.coalesce(func.max(Event.seq), 0)).where(Event.tenant_id == cfg.id)).scalar()
            unread = s.execute(select(Notification).where(Notification.tenant_id == cfg.id,
                                                          Notification.created_at > (_seen(s, c) or utcnow() - timedelta(days=7)))).scalars().all()
            return {"employees": {k: v for k, v in emp_state.items() if k in visible},
                    "active": [_work_brief(w) for w in active], "recent": [_work_brief(w) for w in recent],
                    "approvals": [_approval_json(cfg, a, s) for a in approvals], "sources": _sources(cfg, s),
                    "ai": {"tokens_24h": usage, "budget": cfg.ai.daily_token_budget if c.is_owner else None,
                           "unavailable": LLM.load(s, cfg).unavailable_reason(s)},
                    "unread_notifications": len([n for n in unread if _can_see_notification(s, c, n)]),
                    "seq": seq}

    @app.get("/api/home")
    def home_screen(c: Ctx = Depends(current)):
        with db.session() as s:
            return home.home(s, c.cfg, c.employees(), c.areas())

    @app.get("/api/work")
    def work_list(view: str = "queue", employee: str | None = None, q: str = "", c: Ctx = Depends(current)):
        if view not in ("queue", "investigations"):
            raise HTTPException(400, "view must be queue or investigations")
        visible = c.employees()
        if employee and employee not in visible:
            raise HTTPException(404, "unknown AI employee")
        with db.session() as s:
            return home.work_board(s, c.cfg, visible, view, employee, q[:100], asked_by=None if employee else c.user.id)

    @app.get("/api/work/{work_id}")
    def work_detail(work_id: str, c: Ctx = Depends(current)):
        cfg = c.cfg
        with db.session() as s:
            item = s.execute(select(WorkItem).where(WorkItem.id == work_id, WorkItem.tenant_id == cfg.id)).scalar_one_or_none()
            if item is None or not c.can_see_work(s, item):
                raise HTTPException(404, "work item not found")
            root = item
            while root.parent_id:
                root = s.get(WorkItem, (cfg.id, root.parent_id))
            children = s.execute(select(WorkItem).where(WorkItem.tenant_id == cfg.id, WorkItem.parent_id == root.id)
                                 .order_by(WorkItem.created_at)).scalars().all()
            ids = [root.id] + [ch.id for ch in children]
            evs = s.execute(select(Event).where(Event.tenant_id == cfg.id, Event.work_id.in_(ids)).order_by(Event.seq)).scalars().all()
            evidence = s.execute(select(Evidence).where(Evidence.tenant_id == cfg.id, Evidence.work_id == root.id).order_by(Evidence.id)).scalars().all()
            appr = s.execute(select(Approval).where(Approval.tenant_id == cfg.id, Approval.work_id == root.id).order_by(Approval.requested_at)).scalars().all()
            acts = s.execute(select(ActionRecord).where(ActionRecord.tenant_id == cfg.id, ActionRecord.work_id == root.id)).scalars().all()
            reviews = s.execute(select(FindingReview).where(FindingReview.tenant_id == cfg.id, FindingReview.work_id == root.id)).scalars().all()
            hidden = {e.ref for e in evidence if _withheld_from(c, e.employee)}
            out_root = _withhold(cfg, _work_full(root), hidden)
            return {"item": out_root if item.id == root.id else _work_full(item), "root": out_root,
                    "children": [_work_brief(ch) if not _withheld_from(c, ch.employee) else
                                 {**_work_brief(ch), "title": f"Help from {cfg.employees[ch.employee].name} (withheld)", "headline": None}
                                 for ch in children],
                    "evidence": [{"ref": e.ref, "kind": e.kind, "employee": e.employee, "name": e.name, "withheld": True}
                                 if e.ref in hidden else
                                 {"ref": e.ref, "kind": e.kind, "employee": e.employee, "name": e.name, "args": e.args,
                                  "result": e.result, "freshness": e.freshness, "source": e.source, "records": e.record_count,
                                  "at": _iso(e.created_at)} for e in evidence],
                    "approvals": [_approval_json(cfg, a, s) for a in appr],
                    "actions": [{"action": a.action, "status": a.status, "ref": a.external_ref, "detail": a.detail, "at": _iso(a.created_at)} for a in acts],
                    "reviews": [{"source": r.source, "verdict": r.verdict, "note": r.note, "at": _iso(r.created_at)} for r in reviews],
                    "history": [events.to_dict(e) for e in evs
                                if not _withheld_from(c, (e.payload or {}).get("emp")) or e.type in ("WORK_ITEM_CREATED", "STATE_CHANGED")]}

    # ------------------------------------------------------------------ decisions
    class Decision(BaseModel):
        decision: str = Field(pattern="^(APPROVED|MODIFIED|REJECTED|MORE_INVESTIGATION)$")
        params: dict[str, str | int | float] | None = None
        reason: str = Field("", max_length=1000)

    @app.post("/api/approvals/{approval_id}/decision")
    def decide(approval_id: str, body: Decision, c: Ctx = Depends(current)):
        with db.session() as s:
            user = s.get(User, c.user.id)
            _visible_approval(s, c, approval_id)
            try:
                actions.decide(s, c.cfg, approval_id, user, body.decision, body.params, body.reason)
            except actions.DecisionError as e:
                s.commit()  # keep audit records of denied or expired attempts
                raise HTTPException(e.status, str(e)) from None
            return {"approval": _approval_json(c.cfg, s.get(Approval, (c.cfg.id, approval_id)), s)}

    class Assign(BaseModel):
        user_id: int
        note: str = Field("", max_length=500)

    @app.get("/api/approvals/{approval_id}/assignees")
    def assignees(approval_id: str, c: Ctx = Depends(current)):
        with db.session() as s:
            ap = _visible_approval(s, c, approval_id)
            return {"people": [{"id": u.id, "name": u.name, "role": u.role} for u in actions.approvers_for(s, c.cfg, ap) if u.id != c.user.id]}

    @app.post("/api/approvals/{approval_id}/assign")
    def assign(approval_id: str, body: Assign, c: Ctx = Depends(current)):
        with db.session() as s:
            _visible_approval(s, c, approval_id)
            try:
                actions.assign(s, c.cfg, approval_id, s.get(User, c.user.id), body.user_id, body.note)
            except actions.DecisionError as e:
                raise HTTPException(e.status, str(e)) from None
        return {"ok": True}

    @app.post("/api/approvals/{approval_id}/retry")
    def retry(approval_id: str, c: Ctx = Depends(current)):
        with db.session() as s:
            try:
                actions.retry_action(s, c.cfg, approval_id, s.get(User, c.user.id))
            except actions.DecisionError as e:
                raise HTTPException(e.status, str(e)) from None
            return {"ok": True}

    # ------------------------------------------------------------------ management questions
    class Question(BaseModel):
        text: str = Field(min_length=3, max_length=500)

    @app.post("/api/questions")
    def ask(body: Question, c: Ctx = Depends(current)):
        cfg = c.cfg
        emp = cfg.question_employee()
        with db.session() as s:
            recent = s.execute(select(func.count()).select_from(WorkItem).where(
                WorkItem.tenant_id == cfg.id, WorkItem.created_by == c.user.id, WorkItem.origin == "question",
                WorkItem.created_at >= utcnow() - timedelta(hours=1))).scalar()
            if recent >= QUESTIONS_PER_HOUR:
                raise HTTPException(429, "question limit reached; try again later")
            q = " ".join(body.text.split())
            # What the routine checks have already raised in this person's areas, so an answer can be checked against them
            seen = c.employees()
            alerts = [{"id": w.id, "type": w.type, "title": w.title} for w in s.execute(
                select(WorkItem).where(WorkItem.tenant_id == cfg.id, WorkItem.parent_id.is_(None), WorkItem.origin.in_(("rule", "check")),
                                       WorkItem.status.notin_(list(work.TERMINAL))).order_by(WorkItem.created_at.desc()).limit(200)).scalars()
                      if w.employee in seen][:20]
            item = work.create(s, cfg, employee=emp, type_="MANAGEMENT_QUESTION", priority="MEDIUM", title=q[:120],
                               subject=q, origin="question", created_by=c.user.id, context={"question": q, "open_alerts": alerts},
                               next_step="Investigate the question", msg=f"{c.user.name} asked: “{q[:160]}”")
            return {"work_id": item.id}

    # ------------------------------------------------------------------ activity
    @app.get("/api/events")
    def list_events(after: int = 0, limit: int = 200, c: Ctx = Depends(current)):
        with db.session() as s:
            evs, cursor = _events_after(s, c, after, min(max(limit, 1), 500))
            return {"events": evs, "last": cursor}

    @app.get("/api/events/stream")
    async def stream(request: Request, after: int = 0, c: Ctx = Depends(current)):
        def poll(cursor: int):
            with db.session() as s:
                sess = s.get(UserSession, c.sess.id)
                if sess is None or sess.expires_at < utcnow():
                    return None, cursor
                return _events_after(s, c, cursor, 200)

        async def gen():
            last, idle = after, 0
            while not await request.is_disconnected():
                try:
                    batch, last = await asyncio.to_thread(poll, last)  # database work stays off the event loop
                except OperationalError:  # database restarting: keep the stream open and try again shortly
                    log.warning("live stream: database unavailable, retrying")
                    await asyncio.sleep(2.0)
                    continue
                if batch is None:
                    return  # signed out or session expired
                for ev in batch:
                    yield f"id: {ev['seq']}\ndata: {json.dumps(ev, default=str)}\n\n"
                idle = 0 if batch else idle + 1
                if idle and idle % 15 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1.0)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ operations: pause/kill and health
    class Pause(BaseModel):
        reason: str = Field("", max_length=500)

    def _owner(c: Ctx):
        if not c.is_owner:
            raise HTTPException(403, "owners only")

    @app.post("/api/employees/{employee}/pause")
    def pause(employee: str, body: Pause, c: Ctx = Depends(current)):
        return _set_paused(c, employee, True, body.reason)

    @app.post("/api/employees/{employee}/resume")
    def resume(employee: str, c: Ctx = Depends(current)):
        return _set_paused(c, employee, False, "")

    def _set_paused(c: Ctx, employee: str, paused: bool, reason: str):
        _owner(c)
        if employee not in c.cfg.employees:
            raise HTTPException(404, "unknown AI employee")
        with db.session() as s:
            health.set_paused(s, c.cfg, "employee", employee, paused, s.get(User, c.user.id), reason)
        return {"employee": employee, "paused": paused}

    class ControlChange(BaseModel):
        kind: str = Field(pattern="^(action|source)$")
        target: str = Field(max_length=100)
        paused: bool
        reason: str = Field("", max_length=500)

    @app.get("/api/controls")
    def list_controls(c: Ctx = Depends(current)):
        _owner(c)
        cfg = c.cfg
        with db.session() as s:
            ctl = health.controls(s, cfg)
        return {"actions": [{"id": k, "label": a.label, "area": a.area, **ctl["action"][k]} for k, a in cfg.actions.items()],
                "sources": [{"id": k, "label": src.label or k, "owner": src.owner, **ctl["source"][k]} for k, src in cfg.sources.items()]}

    @app.post("/api/controls")
    def change_control(body: ControlChange, c: Ctx = Depends(current)):
        _owner(c)
        if body.paused and len(body.reason.strip()) < 3:
            raise HTTPException(400, "give a reason for pausing")
        with db.session() as s:
            try:
                health.set_paused(s, c.cfg, body.kind, body.target, body.paused, s.get(User, c.user.id), body.reason)
            except KeyError:
                raise HTTPException(404, "unknown action or data feed") from None
        return {"ok": True}

    @app.get("/api/workforce/health")
    def workforce_health(c: Ctx = Depends(current)):
        with db.session() as s:
            return {"employees": health.employee_health(s, c.cfg, c.employees())}

    # ------------------------------------------------------------------ quality: reviews, usage, comparisons
    class Review(BaseModel):
        verdict: str = Field(pattern="^(CORRECT|PARTLY|WRONG)$")
        note: str = Field("", max_length=1000)

    @app.post("/api/work/{work_id}/review")
    def review(work_id: str, body: Review, c: Ctx = Depends(current)):
        with db.session() as s:
            item = s.get(WorkItem, (c.cfg.id, work_id))
            if item is None or item.employee not in c.employees():
                raise HTTPException(404, "work item not found")
            try:
                quality.review(s, c.cfg, work_id, s.get(User, c.user.id), body.verdict, body.note)
            except quality.ReviewError as e:
                raise HTTPException(400, str(e)) from None
        return {"ok": True}

    @app.get("/api/work/{work_id}/test-draft")
    def test_draft(work_id: str, c: Ctx = Depends(current)):
        _owner(c)
        with db.session() as s:
            try:
                body = quality.draft_scenario(s, c.cfg, work_id)
            except quality.ReviewError as e:
                raise HTTPException(400, str(e)) from None
        return PlainTextResponse(body, media_type="text/yaml",
                                 headers={"Content-Disposition": f'attachment; filename="{quality.scenario_id(work_id)}.yaml"'})

    class Resolve(BaseModel):
        action: str = Field(pattern="^(close|reinvestigate)$")
        note: str = Field("", max_length=1000)

    def _visible_item(s, c: Ctx, work_id: str) -> WorkItem:
        item = s.get(WorkItem, (c.cfg.id, work_id))
        if item is None or (item.employee not in c.employees() and item.created_by != c.user.id):
            raise HTTPException(404, "task not found")
        return item

    @app.post("/api/work/{work_id}/explain")
    def explain(work_id: str, c: Ctx = Depends(current)):
        """Run the AI explanation for an alert that was answered with its verified figures only."""
        with db.session() as s:
            try:
                runtime.request_explanation(s, c.cfg, _visible_item(s, c, work_id), s.get(User, c.user.id))
            except work.WorkError as e:
                raise HTTPException(e.status, str(e)) from None
        return {"ok": True}

    @app.post("/api/work/{work_id}/stop")
    def stop(work_id: str, c: Ctx = Depends(current)):
        with db.session() as s:
            try:
                return {"result": work.request_stop(s, c.cfg, _visible_item(s, c, work_id), s.get(User, c.user.id))}
            except work.WorkError as e:
                raise HTTPException(e.status, str(e)) from None

    @app.post("/api/work/{work_id}/resolve")
    def resolve(work_id: str, body: Resolve, c: Ctx = Depends(current)):
        with db.session() as s:
            try:
                work.resolve_review(s, c.cfg, _visible_item(s, c, work_id), s.get(User, c.user.id), body.action, body.note)
            except work.WorkError as e:
                raise HTTPException(e.status, str(e)) from None
        return {"ok": True}

    @app.get("/api/work/{work_id}/evidence/{ref}/records")
    def evidence_records(work_id: str, ref: str, c: Ctx = Depends(current)):
        """Drill-down to source: the records behind one proof, as stored when the proof was made, with the
        row exactly as the source system sent it. Personal fields are masked for everyone but owners."""
        cfg = c.cfg
        with db.session() as s:
            item = s.get(WorkItem, (cfg.id, work_id))
            if item is None or item.parent_id or not c.can_see_work(s, item):
                raise HTTPException(404, "task not found")
            ev = s.execute(select(Evidence).where(Evidence.tenant_id == cfg.id, Evidence.work_id == work_id, Evidence.ref == ref)).scalar_one_or_none()
            if ev is None or _withheld_from(c, ev.employee):
                raise HTTPException(404, "proof not found")
            recs = s.execute(select(Record).where(Record.tenant_id == cfg.id, Record.id.in_(ev.record_ids or [-1]))
                             .order_by(Record.dataset, Record.occurred_on.desc(), Record.id)).scalars().all()
            out = {}
            for r in recs:
                ds = cfg.datasets.get(r.dataset)
                if ds is None:
                    continue
                hidden = {f for f, spec in ds.fields.items() if spec.sensitive} if not c.is_owner else set()
                cols = {spec.column or f for f, spec in ds.fields.items() if f in hidden}
                declared = {spec.column or f for f, spec in ds.fields.items()}  # columns the pack does not know are never shown
                g = out.setdefault(r.dataset, {"dataset": r.dataset, "label": ds.label, "source": (cfg.sources[ds.source].label or ds.source),
                                               "fields": list(ds.fields), "masked": sorted(hidden), "rows": []})
                g["rows"].append({"data": {k: ("•••" if k in hidden else v) for k, v in r.data.items()},
                                  "raw": {k: ("•••" if k in cols else v) for k, v in (r.raw or {}).items() if k in declared},
                                  "synced_at": _iso(r.synced_at), "changed_since": r.synced_at > ev.created_at})
            return {"ref": ev.ref, "total": ev.record_count, "shown": len(recs), "capped": ev.record_count > len(ev.record_ids or []),
                    "created_at": _iso(ev.created_at), "groups": list(out.values())}

    # ------------------------------------------------------------------ team and temporary access (owners)
    class Grant(BaseModel):
        areas: list[str] = Field(min_length=1, max_length=20)
        hours: int = Field(ge=1, le=security.MAX_GRANT_HOURS)
        reason: str = Field(min_length=5, max_length=500)

    @app.get("/api/team")
    def team(c: Ctx = Depends(current)):
        _owner(c)
        from .models import AccessGrant
        with db.session() as s:
            users = s.execute(select(User).where(User.tenant_id == c.cfg.id).order_by(User.role.desc(), User.name)).scalars().all()
            names = {u.id: u.name for u in users}
            grants = s.execute(select(AccessGrant).where(AccessGrant.tenant_id == c.cfg.id).order_by(AccessGrant.created_at.desc()).limit(100)).scalars().all()
            now = utcnow()
            return {"people": [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "areas": u.areas or [],
                                "approval_limit": u.approval_limit, "active": u.active} for u in users],
                    "grants": [{"id": g.id, "user": names.get(g.user_id), "user_id": g.user_id, "areas": g.areas, "reason": g.reason,
                                "granted_by": names.get(g.granted_by), "expires_at": _iso(g.expires_at), "revoked_at": _iso(g.revoked_at),
                                "active": g.revoked_at is None and g.expires_at > now} for g in grants]}

    @app.post("/api/team/{user_id}/grant")
    def grant(user_id: int, body: Grant, c: Ctx = Depends(current)):
        with db.session() as s:
            try:
                g = security.grant_access(s, c.cfg, s.get(User, c.user.id), user_id, body.areas, body.hours, body.reason)
            except security.AccessError as e:
                raise HTTPException(e.status, str(e)) from None
            return {"id": g.id}

    @app.post("/api/grants/{grant_id}/revoke")
    def revoke(grant_id: int, c: Ctx = Depends(current)):
        with db.session() as s:
            try:
                security.revoke_access(s, c.cfg, s.get(User, c.user.id), grant_id)
            except security.AccessError as e:
                raise HTTPException(e.status, str(e)) from None
        return {"ok": True}

    @app.get("/api/usage")
    def usage(c: Ctx = Depends(current)):
        _owner(c)
        cfg = c.cfg
        with db.session() as s:
            def by(days: int):
                since = utcnow() - timedelta(days=days)
                rows = s.execute(select(LlmUsage.provider, LlmUsage.model, LlmUsage.tier, LlmUsage.task, func.count(),
                                        func.sum(LlmUsage.input_tokens), func.sum(LlmUsage.cache_read_tokens),
                                        func.sum(LlmUsage.output_tokens), func.sum(LlmUsage.cost_usd), func.count(LlmUsage.cost_usd))
                                 .where(LlmUsage.tenant_id == cfg.id, LlmUsage.created_at >= since)
                                 .group_by(LlmUsage.provider, LlmUsage.model, LlmUsage.tier, LlmUsage.task)).all()
                return [{"provider": p, "model": m, "tier": t, "task": k, "calls": n, "input_tokens": int(i or 0),
                         "cached_tokens": int(cr or 0), "output_tokens": int(o or 0),
                         "cost_usd": round(cost, 4) if priced == n and cost is not None else None} for p, m, t, k, n, i, cr, o, cost, priced in rows]
            run = s.execute(select(EvalResult.run_id).where(EvalResult.tenant_id == cfg.id).order_by(EvalResult.created_at.desc()).limit(1)).scalar()
            latest = [{"scenario": r.scenario, "employee": r.employee, "status": r.status, "checks": r.checks,
                       "strong": r.strong_model, "cheap": r.cheap_model, "cost_usd": r.cost_usd}
                      for r in s.execute(select(EvalResult).where(EvalResult.tenant_id == cfg.id, EvalResult.run_id == run)).scalars()] if run else []
            llm = LLM.load(s, cfg)
            ai = llm.cfg.ai
            return {"routes": {"strong": route_label(ai.strong), "cheap": route_label(ai.cheap),
                               "routing": {t: ai.tier_for(t) for t in tenancy.TASKS}},
                    "budget": {"tokens_24h": llm.tokens_today(s), "token_budget": cfg.ai.daily_token_budget,
                               "cost_24h_usd": round(llm.cost_today(s), 4), "cost_budget_usd": cfg.ai.daily_cost_budget_usd},
                    "last_24h": by(1), "last_7d": by(7), "eval_comparison": quality.comparison(s, cfg.id),
                    "needs_test": quality.needs_test(s, cfg),
                    "latest_eval": {"run_id": run, "results": latest}}

    # ------------------------------------------------------------------ AI models and keys (owners)
    class RouteChange(BaseModel):
        tier: str = Field(pattern="^(strong|cheap)$")
        provider: str = Field(max_length=40)
        model: str = Field(max_length=100)
        effort: str = Field("medium", max_length=10)
        price_input: float | None = None
        price_output: float | None = None

    class KeyBody(BaseModel):
        key: str = Field(min_length=8, max_length=500)

    class TierBody(BaseModel):
        tier: str = Field(pattern="^(strong|cheap)$")

    def _settings_call(c: Ctx, fn, *args):
        _owner(c)
        with db.session() as s:
            try:
                fn(s, c.cfg, s.get(User, c.user.id), *args)
            except ai_settings.SettingsError as e:
                raise HTTPException(e.status, str(e)) from None
        return {"ok": True}

    @app.get("/api/ai/settings")
    def ai_overview(c: Ctx = Depends(current)):
        _owner(c)
        with db.session() as s:
            return ai_settings.overview(s, c.cfg)

    @app.post("/api/ai/route")
    def ai_route(body: RouteChange, c: Ctx = Depends(current)):
        return _settings_call(c, ai_settings.set_route, body.tier, body.provider, body.model, body.effort, body.price_input, body.price_output)

    @app.post("/api/ai/route/reset")
    def ai_route_reset(body: TierBody, c: Ctx = Depends(current)):
        return _settings_call(c, ai_settings.reset_route, body.tier)

    @app.post("/api/ai/keys/{provider}")
    def ai_key(provider: str, body: KeyBody, c: Ctx = Depends(current)):
        return _settings_call(c, ai_settings.set_key, provider, body.key)

    @app.post("/api/ai/keys/{provider}/remove")
    def ai_key_remove(provider: str, c: Ctx = Depends(current)):
        return _settings_call(c, ai_settings.remove_key, provider)

    @app.post("/api/ai/test")
    def ai_test(body: TierBody, c: Ctx = Depends(current)):
        """One tiny request to the chosen model, so an owner knows the model name and key work (costs a fraction of a cent)."""
        _owner(c)
        from .llm import LLMRefused, LLMRetryable, LLMUnavailable
        with db.session() as s:
            llm = LLM.load(s, c.cfg)
            label = route_label(llm.route(body.tier))
            why = llm.unavailable_reason(s, body.tier)
            if why:
                return {"ok": False, "model": label, "message": why}
            try:
                llm.create(s, tier=body.tier, task="connection_test", work_id=None, employee="system",
                           system='Reply with the JSON object {"ok": true}.', tools=[], messages=[{"role": "user", "content": "ping"}],
                           schema={"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
                           effort="low")
            except (LLMRetryable, LLMRefused, LLMUnavailable) as e:
                return {"ok": False, "model": label, "message": str(e)}
            return {"ok": True, "model": label, "message": "connected"}

    # ------------------------------------------------------------------ reports and notifications
    @app.get("/api/reports")
    def list_reports(c: Ctx = Depends(current)):
        with db.session() as s:
            rows = s.execute(select(Report).where(Report.tenant_id == c.cfg.id).order_by(Report.created_at.desc()).limit(40)).scalars()
            return {"reports": [{"id": r.id, "kind": r.kind, "date": r.local_date.isoformat(), "created_at": _iso(r.created_at),
                                 "period_start": _iso(r.period_start), "period_end": _iso(r.period_end)} for r in rows]}

    @app.get("/api/reports/{report_id}")
    def get_report(report_id: str, c: Ctx = Depends(current)):
        with db.session() as s:
            r = s.get(Report, (c.cfg.id, report_id))
            if r is None:
                raise HTTPException(404, "report not found")
            content = json.loads(json.dumps(r.content))
            for sec in content["sections"]:  # managers only see items from their own areas
                sec["items"] = [i for i in sec["items"] if _can_see(c, i.get("employee"))]
            if not c.is_owner:  # the overview and AI spend can mention areas a manager does not cover
                content.pop("overview", None)
                content.pop("ai_usage", None)
            return {"id": r.id, "kind": r.kind, "date": r.local_date.isoformat(), "period_start": _iso(r.period_start),
                    "period_end": _iso(r.period_end), "created_at": _iso(r.created_at), **content}

    class ReportRequest(BaseModel):
        kind: str = Field(pattern="^(daily|weekly|monthly)$")

    @app.post("/api/reports")
    def make_report(body: ReportRequest, c: Ctx = Depends(current)):
        _owner(c)
        with db.session() as s:
            r = reports.generate(s, c.cfg, body.kind)
            return {"id": r.id}

    @app.get("/api/notifications")
    def notifications(c: Ctx = Depends(current)):
        with db.session() as s:
            seen = _seen(s, c)
            rows = s.execute(select(Notification).where(Notification.tenant_id == c.cfg.id)
                             .order_by(Notification.created_at.desc()).limit(100)).scalars()
            return {"notifications": [{"id": n.id, "kind": n.kind, "severity": n.severity, "title": n.title, "text": n.text,
                                       "work_id": n.work_id, "employee": n.employee, "created_at": _iso(n.created_at),
                                       "unread": seen is None or n.created_at > seen} for n in rows if _can_see_notification(s, c, n)]}

    @app.post("/api/notifications/seen")
    def notifications_seen(c: Ctx = Depends(current)):
        with db.session() as s:
            s.get(User, c.user.id).notifications_seen_at = utcnow()
        return {"ok": True}

    @app.get("/api/audit/verify")
    def verify(c: Ctx = Depends(current)):
        if not c.is_owner:
            raise HTTPException(403, "owners only")
        with db.session() as s:
            ok, bad = events.verify_chain(s, c.cfg.id)
        return {"intact": ok, "first_broken_seq": bad}

    @app.get("/api/health")
    def healthcheck():
        """API liveness (database reachable) plus the background worker's heartbeat, for monitoring."""
        with db.session() as s:
            s.execute(text("SELECT 1"))
            return {"status": "ok", "worker": health.worker_status(s)}

    # ------------------------------------------------------------------ web app
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    return app


def _events_after(s, c: Ctx, after: int, limit: int, max_scan: int = 5000) -> tuple[list[dict], int]:
    """Up to `limit` events this user may see after `after`, and the last sequence number examined. Filtering
    happens before the limit, so a long run of events from other areas can never stall a manager's feed."""
    visible, out, cursor = c.employees(), [], after
    while len(out) < limit and cursor - after < max_scan:
        rows = s.execute(select(Event).where(Event.tenant_id == c.cfg.id, Event.seq > cursor)
                         .order_by(Event.seq).limit(500)).scalars().all()
        if not rows:
            break
        for e in rows:
            cursor = e.seq
            if e.category == "security" and not c.is_owner:
                continue
            emp = e.payload.get("emp")
            if not c.is_owner and emp and emp not in visible:
                continue
            out.append(events.to_dict(e))
            if len(out) >= limit:
                break
    return out, cursor


def _can_see(c: Ctx, employee: str | None) -> bool:
    """Company-wide items (no employee) are visible to every user; employee items follow the user's areas."""
    return employee is None or employee in c.employees()


def _can_see_notification(s, c: Ctx, n: Notification) -> bool:
    """A decision notice also reaches the people allowed to make that decision."""
    if _can_see(c, n.employee):
        return True
    if n.kind != "decision" or not n.work_id:
        return False
    aps = s.execute(select(Approval).where(Approval.tenant_id == c.cfg.id, Approval.work_id == n.work_id)).scalars()
    return any(c.can_see_approval(a) for a in aps)


def _visible_approval(s, c: Ctx, approval_id: str) -> Approval:
    ap = s.get(Approval, (c.cfg.id, approval_id))
    if ap is None or not c.can_see_approval(ap):
        raise HTTPException(404, "approval not found")
    return ap


def _seen(s, c: Ctx):
    u = s.get(User, c.user.id)
    return u.notifications_seen_at if u else None


def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def _user_json(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role, "areas": u.areas or [], "approval_limit": u.approval_limit}


def _work_brief(w: WorkItem) -> dict:
    return {"id": w.id, "type": w.type, "priority": w.priority, "employee": w.employee, "title": w.title, "status": w.status,
            "next": w.next_step, "parent": w.parent_id, "outcome": w.outcome, "created_at": _iso(w.created_at),
            "completed_at": _iso(w.completed_at), "headline": (w.finding or {}).get("headline")}


def _withheld_from(c: Ctx, employee: str | None) -> bool:
    """Evidence a colleague gathered is part of the task for whoever may see the task, except what a sensitive employee
    (HR) gathered: that stays with the people who cover its area, even when another employee asked for it."""
    e = c.cfg.employees.get(employee) if employee else None
    return bool(e and e.sensitive and employee not in c.employees())


def _withhold(cfg, w: dict, hidden: set[str]) -> dict:
    """A finding as this viewer may see it: statements resting on evidence from areas they do not cover are replaced by
    a note, and so are the headline and impact (which may repeat those figures)."""
    f = w.get("finding")
    if not hidden or not f:
        return w
    sts, gone = [], False
    for st in f.get("statements", []):
        if set(st.get("refs") or []) & hidden:
            gone = True
            sts.append({"kind": st.get("kind"), "text": "Withheld: this uses figures from an area you do not cover.", "refs": [],
                        "withheld": True})
        else:
            sts.append(st)
    if not gone:
        return w
    rec = f.get("recommendation")
    if rec and set(rec.get("refs") or []) & hidden:
        rec = {**rec, "rationale": "Withheld: it uses figures from an area you do not cover."}
    visible = [s["text"] for s in sts if not s.get("withheld") and s.get("kind") == "FACT"]
    return {**w, "finding": {**f, "statements": sts, "recommendation": rec, "impact": "",
                             "headline": visible[0] if visible else "Part of this answer uses figures from areas you do not cover."}}


def _work_full(w: WorkItem) -> dict:
    return {**_work_brief(w), "subject": w.subject, "context": {k: v for k, v in w.context.items() if k != "evidence_pack"},
            "finding": w.finding, "origin": w.origin, "attempts": w.attempts, "error": w.error if w.status != "COMPLETED" else "",
            "stop_requested": w.stop_requested}


def _approval_json(cfg, a: Approval, s) -> dict:
    act = cfg.actions.get(a.action)
    assignee = s.get(User, a.assigned_to) if a.assigned_to else None
    return {"id": a.id, "work_id": a.work_id, "employee": a.employee, "action": a.action, "action_label": act.label if act else a.action,
            "level": act.level if act else None, "approvers": [ap.model_dump() for ap in act.approvers] if act else [],
            "params": a.params, "final_params": a.final_params, "amount": a.amount, "title": a.title, "rationale": a.rationale,
            "status": a.status, "round": a.round, "requested_at": _iso(a.requested_at), "decided_at": _iso(a.decided_at),
            "reason": a.reason, "assigned_to": a.assigned_to, "assigned_to_name": assignee.name if assignee else None}


def _area_data(cfg) -> dict[str, list[str]]:
    """Datasets each area's tools read, so the inspector can show an area's data sources and their freshness."""
    out: dict[str, set] = {}
    for t in cfg.tools.values():
        names = {t.dataset} if t.dataset else set()
        todo = list(t.metrics)
        while todo:
            m = cfg.metrics[todo.pop()]
            if m.dataset:
                names.add(m.dataset)
            else:
                todo.extend(expr.names(m.formula))
        out.setdefault(t.area, set()).update(names)
    return {k: sorted(v) for k, v in out.items()}


def _sources(cfg, s) -> list[dict]:
    out = []
    for sid, src in cfg.sources.items():
        st = s.get(SourceState, (cfg.id, sid))
        dsets = [d for d, ds in cfg.datasets.items() if ds.source == sid]
        fresh = [data.freshness(s, cfg, d)[0] for d in dsets]
        out.append({"id": sid, "label": src.label or sid, "status": st.status if st else "NEVER",
                    "last_success": _iso(st.last_success) if st else None, "error": st.error if st else "",
                    "freshness": "STALE" if "STALE" in fresh else ("NEVER_SYNCED" if "NEVER_SYNCED" in fresh else "CURRENT"),
                    "datasets": [cfg.datasets[d].label for d in dsets]})
    return out


def main(host: str = "0.0.0.0", port: int = 8000):
    import uvicorn
    s = get_settings()
    # Behind a reverse proxy in production, trust forwarded headers only from the proxy on this host.
    uvicorn.run(create_app(), host=host, port=port, proxy_headers=True,
                forwarded_allow_ips="*" if s.environment != "production" else "127.0.0.1",
                timeout_graceful_shutdown=10)  # open live-activity streams must not block a restart
