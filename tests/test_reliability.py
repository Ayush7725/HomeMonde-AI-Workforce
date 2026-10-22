"""Production reliability: work is claimed once, decisions reach the people allowed to make them, live feeds never
stall, stale decisions expire on their own and notifications never hold database locks while being delivered."""
from datetime import timedelta

from sqlalchemy import select

from conftest import stockout_scenario, use_fake
from test_workforce import _item, _login, _run_next, _stockout_script, _sync_and_rules


def _purchase_request(env) -> tuple[str, str]:
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    wid = _run_next(env)
    from workforce import db
    from workforce.models import Approval
    with db.session() as s:
        return wid, s.execute(select(Approval).where(Approval.work_id == wid)).scalar_one().id


def _add_user(email: str, role: str, areas: list[str], limit: float | None = None):
    from workforce import db, security
    from workforce.models import User
    with db.session() as s:
        s.add(User(tenant_id="homemonde", email=email, name=email.split("@")[0], role=role, areas=areas, approval_limit=limit,
                   password_hash=security.hash_password("correct horse battery")))


def test_a_leased_item_is_not_claimed_twice(env):
    from workforce import db, work
    from workforce.models import WorkItem
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    with db.session() as s:
        first = work.claim(s, ["homemonde"], 600)
        assert first is not None
        wid = first.id
    with db.session() as s:  # still QUEUED (its runner has not started yet) but leased: nobody else may take it
        assert s.get(WorkItem, ("homemonde", wid)).status == "QUEUED"
        assert work.claim(s, ["homemonde"], 600) is None
    with db.session() as s:  # the worker died before starting: once the lease runs out the item is claimable again
        s.get(WorkItem, ("homemonde", wid)).lease_until = db.utcnow() - timedelta(seconds=1)
    with db.session() as s:
        again = work.claim(s, ["homemonde"], 600)
        assert again is not None and again.id == wid and again.attempts == 2


def test_the_manager_who_may_approve_a_purchase_request_can_see_and_decide_it(env, client):
    wid, apid = _purchase_request(env)
    _add_user("buyer@homemonde.in", "manager", ["procurement"], 100000)
    csrf = _login(client, "buyer@homemonde.in")
    st = client.get("/api/state").json()
    assert [a["id"] for a in st["approvals"]] == [apid]
    assert client.get(f"/api/work/{wid}").status_code == 200  # the evidence behind the decision is visible too
    over = client.post(f"/api/approvals/{apid}/decision", json={"decision": "APPROVED"}, headers={"X-CSRF-Token": csrf})
    assert over.status_code == 403  # 129,600 is above this manager's limit
    ok = client.post(f"/api/approvals/{apid}/decision", json={"decision": "MODIFIED", "params": {"quantity": 600}},
                     headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 200 and ok.json()["approval"]["status"] == "MODIFIED"
    assert _item(wid).outcome == "ACTION_TAKEN"


def test_decisions_stay_hidden_from_managers_of_other_areas(env, client):
    wid, apid = _purchase_request(env)
    _login(client, "sales.head@homemonde.in")
    assert client.get("/api/state").json()["approvals"] == []
    assert client.get(f"/api/work/{wid}").status_code == 404
    assert client.get(f"/api/approvals/{apid}/assignees").status_code == 404


def test_a_long_run_of_other_areas_events_never_stalls_a_managers_feed(env, client):
    from workforce import db, events
    with db.session() as s:
        for i in range(700):
            events.emit(s, "homemonde", "STATE_CHANGED", f"inventory step {i}", actor="inventory_ai", emp="inventory_ai")
        events.emit(s, "homemonde", "STATE_CHANGED", "sales step", actor="sales_ai", emp="sales_ai")
    _login(client, "sales.head@homemonde.in")
    page = client.get("/api/events?after=0&limit=10").json()
    assert [e["msg"] for e in page["events"]] == ["sales step"]
    assert page["last"] >= 701


def test_decisions_nobody_made_expire_on_their_own(env):
    wid, apid = _purchase_request(env)
    from workforce import actions, db
    from workforce.models import Approval
    with db.session() as s:
        assert actions.expire_stale(s, env.cfg) == 0
        s.get(Approval, ("homemonde", apid)).requested_at = db.utcnow() - actions.APPROVAL_TTL - timedelta(minutes=1)
    with db.session() as s:
        assert actions.expire_stale(s, env.cfg) == 1
    with db.session() as s:
        assert s.get(Approval, ("homemonde", apid)).status == "EXPIRED"
    assert _item(wid).outcome == "EXPIRED"


def test_notifications_are_recorded_first_and_delivered_afterwards(env):
    from workforce import db, notify
    from workforce.models import Notification
    with db.session() as s:
        notify.send(s, env.cfg, kind="finding", severity="HIGH", title="Stock running out", verified="HM-204 has 2 days of stock.")
    with db.session() as s:
        n = s.execute(select(Notification)).scalar_one()
        assert n.delivered == "pending" and n.text == "HM-204 has 2 days of stock."  # visible in the app straight away
    assert notify.deliver_pending(env.cfg) == 1
    assert notify.deliver_pending(env.cfg) == 0
    with db.session() as s:
        n = s.execute(select(Notification)).scalar_one()
        assert n.delivered == "in_app" and n.text == "HM-204 has 2 days of stock."  # no AI key: the verified wording stays


def test_old_daily_snapshots_are_pruned_so_the_database_stays_bounded(env):
    from workforce import data, db
    from workforce.models import Record
    stockout_scenario(env.exports, env.cfg)
    old_day = data.today(env.cfg) - timedelta(days=env.cfg.datasets["inventory"].keep_snapshot_days + 1)
    with db.session() as s:
        s.add(Record(tenant_id="homemonde", dataset="inventory", key=f"HM-OLD|{old_day.isoformat()}", occurred_on=old_day,
                     data={"sku": "HM-OLD"}, raw={}, synced_at=db.utcnow()))
    _sync_and_rules(env)
    with db.session() as s:
        days = set(s.execute(select(Record.occurred_on).where(Record.dataset == "inventory")).scalars())
        assert days == {data.today(env.cfg)}


def test_an_action_cut_off_mid_run_becomes_a_visible_failure_that_can_be_retried(env):
    wid, apid = _purchase_request(env)
    from workforce import actions, db
    from workforce.models import ActionRecord, User
    with db.session() as s:
        owner = User(tenant_id="homemonde", email="o@x.in", name="Founder", role="owner", password_hash="x")
        s.add(owner)
        s.flush()
        actions.decide(s, env.cfg, apid, owner, "APPROVED")
        rec = s.execute(select(ActionRecord)).scalar_one()
        assert rec.status == "VERIFIED"
        rec.status, rec.created_at = "PENDING", db.utcnow() - actions.STUCK_AFTER - timedelta(minutes=1)  # as if the run died
    with db.session() as s:
        assert actions.fail_unconfirmed(s, env.cfg) == 1
    with db.session() as s:
        assert s.execute(select(ActionRecord)).scalar_one().status == "FAILED"
        actions.retry_action(s, env.cfg, apid, s.execute(select(User)).scalar_one())
    with db.session() as s:
        assert s.execute(select(ActionRecord)).scalar_one().status == "VERIFIED"
    assert _item(wid).outcome == "ACTION_TAKEN"


def test_health_reports_whether_the_worker_is_alive(env, client):
    from workforce import db, health
    assert client.get("/api/health").json()["worker"]["status"] == "missing"
    with db.session() as s:
        health.beat(s, "worker:test", db.utcnow())
    assert client.get("/api/health").json()["worker"]["status"] == "ok"
    with db.session() as s:
        s.get(health.Heartbeat, "worker:test").beat_at = db.utcnow() - health.WORKER_LATE_AFTER - timedelta(seconds=5)
    assert client.get("/api/health").json()["worker"]["status"] == "late"


def test_a_rule_people_keep_turning_down_is_proposed_for_review_never_changed(env):
    from workforce import db, quality, work
    from workforce.models import FindingReview, User, WorkItem
    with db.session() as s:
        owner = User(tenant_id="homemonde", email="o@x.in", name="Founder", role="owner", password_hash="x")
        s.add(owner)
        s.flush()
        for i in range(6):
            it = work.create(s, env.cfg, employee="logistics_ai", type_="CARRIER_DELAYS", title=f"Late deliveries {i}", origin="rule",
                             rule_id="carrier_delays", status="COMPLETED", context={"group": f"C{i}"})
            it.finding = {"outcome": "FINDING", "statements": []}
            s.add(FindingReview(tenant_id="homemonde", work_id=it.id, employee="logistics_ai", source="human",
                                verdict="WRONG" if i < 4 else "CORRECT", reviewer=owner.id))
    with db.session() as s:
        assert quality.rule_feedback(s, env.cfg)["carrier_delays"] == {"alerts": 6, "judged": 6, "turned_down": 4, "share_turned_down": 0.667}
        assert quality.propose_rule_reviews(s, env.cfg) == 1
    with db.session() as s:
        assert quality.propose_rule_reviews(s, env.cfg) == 0  # one open review at a time
        review = s.execute(select(WorkItem).where(WorkItem.type == "RULE_REVIEW")).scalar_one()
        assert review.status == "NEEDS_HUMAN_REVIEW" and "4" in review.finding["headline"]
    assert env.cfg.rules[[r.id for r in env.cfg.rules].index("carrier_delays")].when == "late_share_14d > 0.2 and shipments_14d >= 30"


def test_a_database_outage_is_a_clear_retryable_answer_not_a_server_error(env, client, monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy.exc import OperationalError

    from workforce import db

    @contextmanager
    def down():
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))
        yield

    monkeypatch.setattr(db, "session", down)
    r = client.get("/api/health")
    assert r.status_code == 503 and r.headers["Retry-After"] == "5" and "try again" in r.json()["detail"]
