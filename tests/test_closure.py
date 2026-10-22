"""The remaining locked items: stop/limits/pauses, drill-down to source records, opportunities, home and work
views, assignment, trucks, scheduled reviews, temporary access, override rate, test drafts, evidence-based
certainty with review gates, data problems as owned tasks."""

import pytest
from sqlalchemy import select

from conftest import stockout_scenario, text, tool_use, use_fake
from test_workforce import _item, _login, _run_next, _stockout_script, _sync_and_rules


def _owner(s):
    from workforce import security
    from workforce.models import User
    u = s.execute(select(User).where(User.email == "o@x.in")).scalar_one_or_none()
    if u is None:
        u = User(tenant_id="homemonde", email="o@x.in", name="Founder", role="owner", password_hash=security.hash_password("correct horse battery"))
        s.add(u)
        s.flush()
    return u


def _answer(statements, quality="LIKELY", rec=None):
    return {"outcome": "FINDING", "headline": "HM-204 will run out soon", "statements": statements, "evidence_quality": quality,
            "impact": "", "unknowns": [], "recommendation": rec or {"action": "none", "summary": "", "rationale": "", "params": [], "refs": []}}


# ---------------------------------------------------------------- item 11: stop, limits, pauses

def test_stop_a_waiting_task_and_a_running_one(env):
    from workforce import db, work
    from workforce.models import WorkItem
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    with db.session() as s:
        item = s.execute(select(WorkItem)).scalar_one()
        assert work.request_stop(s, env.cfg, item, _owner(s)) == "stopped"
        assert item.status == "COMPLETED" and item.outcome == "STOPPED"
        with pytest.raises(work.WorkError):
            work.request_stop(s, env.cfg, item, _owner(s))
    # a running task stops at its next step; the request is made from another session mid-investigation
    with db.session() as s:
        item.status, item.outcome, item.completed_at = "QUEUED", None, None
        s.merge(item)

    def first(kw):
        with db.session() as other:
            it = other.execute(select(WorkItem)).scalar_one()
            assert it.status == "INVESTIGATING"  # progress is saved step by step, visible to other users
            assert work.request_stop(other, env.cfg, it, _owner(other)) == "stopping"
        return [tool_use("get_inventory_position", {"sku": "HM-204"}, 1)]

    fake = use_fake([first])
    wid = _run_next(env)
    assert len(fake.calls) == 1 and _item(wid).outcome == "STOPPED"


def test_cost_limit_ends_an_investigation_and_asks_a_person(env):
    from workforce import tenancy
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    env.cfg = env.cfg.model_copy(update={"ai": env.cfg.ai.model_copy(update={"max_investigation_cost_usd": 0.001})})
    tenancy.get.cache_clear()
    from workforce import db, runtime, work
    from workforce.models import WorkItem
    fake = use_fake([lambda kw: [tool_use("get_inventory_position", {"sku": "HM-204"}, 1)]])
    with db.session() as s:
        wid = work.claim(s, ["homemonde"], 600).id
    with db.session() as s:
        runtime.run(s, env.cfg, s.get(WorkItem, ("homemonde", wid)))
    it = _item(wid)
    assert len(fake.calls) == 1  # the first call cost more than the limit; no second call was made
    assert it.status == "NEEDS_HUMAN_REVIEW" and "spending limit" in it.finding["unknowns"][0]


def test_time_limit_forces_a_conclusion(env, monkeypatch):
    import workforce.runtime as rt
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    ticks = []
    monkeypatch.setattr(rt.time, "monotonic", lambda: ticks.append(1) or (0 if len(ticks) == 1 else 1000))
    none = {"outcome": "INSUFFICIENT_EVIDENCE", "headline": "", "statements": [], "evidence_quality": "INSUFFICIENT_EVIDENCE",
            "impact": "", "unknowns": [], "recommendation": {"action": "none", "summary": "", "rationale": "", "params": [], "refs": []}}
    fake = use_fake([lambda kw: [text(none)]])
    wid = _run_next(env)
    assert "Time limit reached" in fake.calls[0]["messages"][-1]["content"]
    assert fake.calls[0].get("tool_choice") == {"type": "none"}
    assert _item(wid).status != "QUEUED"


def test_paused_action_type_never_executes_and_paused_feed_is_not_synced(env):
    from workforce import actions, data, db, health
    from workforce.models import Approval
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    wid = _run_next(env)
    with db.session() as s:
        owner = _owner(s)
        health.set_paused(s, env.cfg, "action", "create_purchase_request", True, owner, "supplier audit")
        health.set_paused(s, env.cfg, "source", "exports", True, owner, "export broken")
        assert "exports" not in data.sources_due(s, env.cfg)
        ap = s.execute(select(Approval).where(Approval.work_id == wid)).scalar_one()
        with pytest.raises(actions.DecisionError, match="paused"):
            actions.decide(s, env.cfg, ap.id, owner, "APPROVED")
    with db.session() as s:
        assert s.get(Approval, ("homemonde", ap.id)).status == "PENDING"  # the decision did not go through
        assert health.controls(s, env.cfg)["action"]["create_purchase_request"]["paused"] is True


def test_a_person_closes_or_reopens_a_task_waiting_for_review(env):
    from workforce import db, work
    from workforce.models import WorkItem
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    with db.session() as s:
        item = s.execute(select(WorkItem)).scalar_one()
        with pytest.raises(work.WorkError):
            work.resolve_review(s, env.cfg, item, _owner(s), "close")
        item.status = "NEEDS_HUMAN_REVIEW"
        work.resolve_review(s, env.cfg, item, _owner(s), "reinvestigate", "check the Amazon sale")
        assert item.status == "QUEUED" and item.context["human_request"] == "check the Amazon sale"
        item.status = "NEEDS_HUMAN_REVIEW"
        work.resolve_review(s, env.cfg, item, _owner(s), "close", "known issue")
        assert item.outcome == "REVIEWED"


# ---------------------------------------------------------------- items 15 and 5: raw records and drill-down

def test_proof_drills_down_to_the_source_records_as_received(env, client):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    wid = _run_next(env)
    _login(client, "founder@homemonde.in")
    detail = client.get(f"/api/work/{wid}").json()
    by_ref = {e["ref"]: e for e in detail["evidence"]}
    inv = client.get(f"/api/work/{wid}/evidence/E1/records").json()  # stock lookup
    rows = inv["groups"][0]["rows"]
    assert inv["groups"][0]["label"] == "Inventory (WMS)" and len(rows) == 1
    assert rows[0]["data"]["available"] == 320 and rows[0]["raw"]["available"] == "320"  # normalised and as received
    demand = client.get(f"/api/work/{wid}/evidence/E2/records").json()  # 7- and 30-day demand: every order line counted
    assert demand["total"] == demand["shown"] == 90  # 30 days x 3 channels of HM-204 order lines, every one counted
    assert by_ref["E2"]["name"] == "get_sales_velocity"
    seeded = next(r for r, e in by_ref.items() if e["name"] == "stock_coverage_days")
    cov = client.get(f"/api/work/{wid}/evidence/{seeded}/records").json()  # derived metric: stock rows + order lines
    assert {g["dataset"] for g in cov["groups"]} == {"inventory", "order_lines"}


def test_drill_down_masks_personal_fields_for_managers(env, client):
    from workforce import db, security
    from workforce.models import User
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    wid = _run_next(env)
    with db.session() as s:
        s.add(User(tenant_id="homemonde", email="wh@homemonde.in", name="Warehouse Head", role="manager", areas=["warehouse"],
                   password_hash=security.hash_password("correct horse battery")))
    _login(client, "wh@homemonde.in")
    rows = client.get(f"/api/work/{wid}/evidence/E2/records").json()["groups"][0]["rows"]
    assert all(r["data"]["customer_email"] == "•••" and r["raw"]["customer_email"] == "•••" for r in rows)
    _login(client, "founder@homemonde.in")
    rows = client.get(f"/api/work/{wid}/evidence/E2/records").json()["groups"][0]["rows"]
    assert all("@example.com" in r["data"]["customer_email"] for r in rows)


# ---------------------------------------------------------------- items 1, 2 and 8: home screen, good news, tailored figures

def _demo_homemonde(env, monkeypatch, tmp_path):
    import sys

    from conftest import ROOT
    sys.path.insert(0, str(ROOT / "demo"))
    from generate import homemonde
    for k, v in homemonde(tmp_path / "data").items():
        monkeypatch.setenv(k, v)
    from workforce import data, db, rules
    for sid in env.cfg.sources:
        with db.session() as s:
            data.sync_source(s, env.cfg, sid)
    with db.session() as s:
        rules.evaluate(s, env.cfg)


def test_home_shows_figures_attention_decisions_good_news_and_done_today(env, client, monkeypatch, tmp_path):
    from workforce import db, runtime, work
    from workforce.models import WorkItem
    _demo_homemonde(env, monkeypatch, tmp_path)
    while True:  # no AI: every task finishes with verified facts
        with db.session() as s:
            it = work.claim(s, ["homemonde"], 600)
            wid = it.id if it else None
        if not wid:
            break
        with db.session() as s:
            runtime.run(s, env.cfg, s.get(WorkItem, ("homemonde", wid)))
    _login(client, "founder@homemonde.in")
    h = client.get("/api/home").json()
    figures = {k["metric"]: k for k in h["kpis"]}
    assert set(figures) == {"sales_7d", "profit_30d", "plant_achievement_7d", "all_roas_7d", "all_late_share_14d", "all_absence_7d"}
    assert figures["profit_30d"]["trend"] == "down" and figures["profit_30d"]["good"] is False
    assert figures["all_late_share_14d"]["trend"] == "up" and figures["all_late_share_14d"]["good"] is False  # more late = bad
    assert figures["plant_achievement_7d"]["good"] is False
    titles = [a["title"] for a in h["attention"]]
    assert "Stockout risk · HM-204" in titles and "Profit fell against the previous 30 days" in titles
    assert "Purchase order overdue · PO-7731" in titles  # the late fabric order behind the blocked batch
    assert "Invoice RLC-2291 does not match its order · Rajasthan Linen Co" in titles  # short delivery billed in full
    assert "Possible duplicate invoice BCM-0457 · Bagru Cotton Mills" in titles
    assert all("growing" not in t for t in titles)  # good news is never shown as a problem
    assert [g["title"] for g in h["good_news"]] == ["Sales growing · d2c"]
    assert h["done_today_total"] == 10 and h["in_progress"] == 0
    _login(client, "sales.head@homemonde.in")  # a sales manager sees only the sales figure and sales work
    h = client.get("/api/home").json()
    assert [k["metric"] for k in h["kpis"]] == ["sales_7d"]
    assert {a["employee"] for a in h["attention"] + h["good_news"] + h["done_today"]} <= {"sales_ai"}


# ---------------------------------------------------------------- items 3 and 4: work queue and investigations list

def test_work_screen_lists_the_queue_and_searchable_investigations(env, client):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    _login(client, "founder@homemonde.in")
    q = client.get("/api/work?view=queue").json()
    assert q["counts"]["waiting_to_start"] == 1 and [i["title"] for i in q["items"]] == ["Stockout risk · HM-204"]
    use_fake(_stockout_script())
    _run_next(env)
    q = client.get("/api/work?view=queue").json()
    assert q["counts"]["waiting_for_you"] == 1 and q["items"][0]["status"] == "NEEDS_HUMAN_REVIEW"
    from workforce import actions, db
    from workforce.models import Approval
    with db.session() as s:
        actions.decide(s, env.cfg, s.execute(select(Approval)).scalar_one().id, _owner(s), "REJECTED")
    inv = client.get("/api/work?view=investigations").json()
    assert inv["total"] == 1 and inv["items"][0]["headline"]
    assert client.get("/api/work?view=investigations&q=cushion").json()["total"] == 0
    assert client.get("/api/work?view=investigations&q=HM-204").json()["total"] == 1
    assert client.get("/api/work?view=investigations&employee=sales_ai").json()["total"] == 0
    _login(client, "sales.head@homemonde.in")
    assert client.get("/api/work?view=investigations").json()["total"] == 0  # not in a sales manager's area
    assert client.get("/api/work?view=queue&employee=inventory_ai").status_code == 404


# ---------------------------------------------------------------- item 6: hand a decision to someone else

def test_hand_a_decision_to_someone_allowed_to_make_it(env, client):
    from workforce import actions, db, security
    from workforce.models import Approval, User
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    _run_next(env)
    with db.session() as s:
        s.add_all([User(tenant_id="homemonde", email="proc@homemonde.in", name="Procurement Head", role="manager", areas=["procurement"],
                        approval_limit=200000, password_hash=security.hash_password("correct horse battery")),
                   User(tenant_id="homemonde", email="cofounder@homemonde.in", name="Co-founder", role="owner",
                        password_hash=security.hash_password("correct horse battery"))])
        apid = s.execute(select(Approval)).scalar_one().id  # a purchase of 1,29,600
    csrf = _login(client, "founder@homemonde.in")
    people = {p["name"] for p in client.get(f"/api/approvals/{apid}/assignees").json()["people"]}
    # managers may approve purchases only up to 1,00,000 in HomeMonde's rules, whatever their personal limit
    assert people == {"Co-founder"}
    with db.session() as s:
        head = s.execute(select(User).where(User.email == "proc@homemonde.in")).scalar_one().id
        cof = s.execute(select(User).where(User.email == "cofounder@homemonde.in")).scalar_one().id
    h = {"X-CSRF-Token": csrf}
    assert client.post(f"/api/approvals/{apid}/assign", json={"user_id": head}, headers=h).status_code == 403
    assert client.post(f"/api/approvals/{apid}/assign", json={"user_id": cof, "note": "your call"}, headers=h).status_code == 200
    detail = client.get(f"/api/work/{client.get('/api/state').json()['approvals'][0]['work_id']}").json()
    assert detail["approvals"][0]["assigned_to_name"] == "Co-founder" and detail["approvals"][0]["status"] == "PENDING"
    with db.session() as s:
        actions.decide(s, env.cfg, apid, s.get(User, cof), "APPROVED")
        assert s.get(Approval, ("homemonde", apid)).status == "APPROVED"


# ---------------------------------------------------------------- item 7: trucks follow real new data

def test_sync_reports_only_genuinely_new_rows(env):
    import csv

    from workforce import data, db
    from workforce.models import Event
    stockout_scenario(env.exports, env.cfg)
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")  # same files again: nothing new
    path = env.exports / "order_lines.csv"
    rows = list(csv.DictReader(path.open()))
    rows.append({**rows[0], "order_id": "NEW1"})
    from conftest import write_csv
    write_csv(path, rows)
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
    with db.session() as s:
        syncs = [e.payload for e in s.execute(select(Event).where(Event.type == "DATA_SYNC").order_by(Event.seq)).scalars()]
    assert [p["new_rows"]["order_lines"] for p in syncs] == [len(rows) - 1, 0, 1]
    assert "inventory" not in syncs[0]["new_rows"]  # snapshot data has no "new" rows, only a fresh picture


def test_layout_rejects_a_truck_linked_to_unknown_data(env):
    import yaml

    from workforce import tenancy
    pack = yaml.safe_load(tenancy.pack_path("homemonde").read_text())
    pack["layout"]["props"].append({"type": "truck", "at": [1, 1], "dataset": "nope"})
    with pytest.raises(ValueError, match="dataset only applies to trucks"):
        tenancy.TenantConfig.model_validate(pack)


# ---------------------------------------------------------------- item 9: scheduled reviews

def test_scheduled_reviews_run_once_per_period_in_company_time(env):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from workforce import db, rules
    from workforce.models import WorkItem
    ist = ZoneInfo("Asia/Kolkata")
    monday_early = datetime(2026, 10, 5, 7, 0, tzinfo=ist)  # a Monday, 1 October was day 1 of the month
    monday_late = datetime(2026, 10, 5, 11, 0, tzinfo=ist)
    with db.session() as s:
        assert rules.schedules_due(s, env.cfg, monday_early) == 0  # nothing before its hour
        assert rules.schedules_due(s, env.cfg, monday_late) == 4  # daily sales, daily stock, weekly business, weekly suppliers
        assert rules.schedules_due(s, env.cfg, monday_late) == 0  # never twice in the same period
        assert rules.schedules_due(s, env.cfg, datetime(2026, 10, 6, 11, 0, tzinfo=ist)) == 2  # Tuesday: only the daily ones
        assert rules.schedules_due(s, env.cfg, datetime(2026, 11, 1, 11, 0, tzinfo=ist)) == 4  # 1 Nov (Sunday): daily + monthly
        items = s.execute(select(WorkItem).where(WorkItem.origin == "schedule")).scalars().all()
        assert {i.title for i in items} >= {"Weekly supplier review · 2026-W41", "Monthly finance review · 2026-11"}
        assert all(i.context["question"] for i in items)


def test_a_scheduled_review_is_investigated_on_the_strong_model(env):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from workforce import db, rules
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    from workforce import work
    with db.session() as s:
        for it in s.execute(select(work.WorkItem)).scalars():
            it.status = "COMPLETED"
        rules.schedules_due(s, env.cfg.model_copy(update={"schedules": [env.cfg.schedules[1]]}),
                            datetime(2026, 10, 5, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    fake = use_fake([lambda kw: [text({"outcome": "INSUFFICIENT_EVIDENCE", "headline": "", "statements": [], "evidence_quality": "INSUFFICIENT_EVIDENCE",
                                       "impact": "", "unknowns": [], "recommendation": {"action": "none", "summary": "", "rationale": "", "params": [], "refs": []}})]])
    _run_next(env)
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    assert "regular_review" in fake.calls[0]["messages"][0]["content"]


# ---------------------------------------------------------------- item 10: temporary extra access

def test_temporary_access_is_owner_only_time_limited_audited_and_revocable(env, client):
    from datetime import timedelta

    from workforce import db, security
    from workforce.models import AccessGrant, Event, User
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    wid = _run_next(env)  # a warehouse investigation
    with db.session() as s:
        s.add(User(tenant_id="homemonde", email="fin@homemonde.in", name="Finance Head", role="manager", areas=["finance"],
                   password_hash=security.hash_password("correct horse battery")))
        s.flush()
        fin = s.execute(select(User).where(User.email == "fin@homemonde.in")).scalar_one().id
    csrf = _login(client, "fin@homemonde.in")
    assert client.get(f"/api/work/{wid}").status_code in (403, 404)
    assert client.post(f"/api/team/{fin}/grant", json={"areas": ["warehouse"], "hours": 4, "reason": "month-end stock check"},
                       headers={"X-CSRF-Token": csrf}).status_code == 403  # cannot give themselves access
    csrf = _login(client, "founder@homemonde.in")
    h = {"X-CSRF-Token": csrf}
    assert client.post(f"/api/team/{fin}/grant", json={"areas": ["moon"], "hours": 4, "reason": "month-end stock check"}, headers=h).status_code == 400
    assert client.post(f"/api/team/{fin}/grant", json={"areas": ["warehouse"], "hours": 500, "reason": "month-end"}, headers=h).status_code == 422
    r = client.post(f"/api/team/{fin}/grant", json={"areas": ["warehouse"], "hours": 4, "reason": "month-end stock check"}, headers=h)
    assert r.status_code == 200
    gid = r.json()["id"]
    team = client.get("/api/team").json()
    assert team["grants"][0]["active"] and team["grants"][0]["user"] == "Finance Head"
    _login(client, "fin@homemonde.in")
    assert client.get(f"/api/work/{wid}").status_code == 200  # can now see warehouse work
    assert client.get("/api/team").status_code == 403
    # expiry needs no action from anyone
    with db.session() as s:
        s.get(AccessGrant, gid).expires_at = s.get(AccessGrant, gid).created_at - timedelta(minutes=1)
    assert client.get(f"/api/work/{wid}").status_code in (403, 404)
    with db.session() as s:
        s.get(AccessGrant, gid).expires_at = s.get(AccessGrant, gid).created_at + timedelta(hours=4)
    assert client.get(f"/api/work/{wid}").status_code == 200
    h = {"X-CSRF-Token": _login(client, "founder@homemonde.in")}
    assert client.post(f"/api/grants/{gid}/revoke", headers=h).status_code == 200
    _login(client, "fin@homemonde.in")
    assert client.get(f"/api/work/{wid}").status_code in (403, 404)
    with db.session() as s:
        types = [e.type for e in s.execute(select(Event).where(Event.category == "security")).scalars()]
    assert "ACCESS_GRANTED" in types and "ACCESS_REVOKED" in types


# ---------------------------------------------------------------- item 12: overrides, wrong answers become tests, risk-based testing

def test_override_rate_counts_changed_rejected_and_sent_back_recommendations(env):
    from datetime import timedelta

    from workforce import db, health, quality
    from workforce.db import utcnow
    from workforce import work
    from workforce.models import Approval
    with db.session() as s:
        for i, st in enumerate(["APPROVED", "APPROVED", "MODIFIED", "REJECTED", "MORE_INVESTIGATION", "PENDING"]):
            w = work.create(s, env.cfg, employee="inventory_ai", type_="STOCKOUT_RISK", title="t", origin="rule")
            s.add(Approval(tenant_id="homemonde", id=f"AP-{i}", work_id=w.id, employee="inventory_ai", action="create_purchase_request",
                           params={}, title="t", status=st, decided_at=None if st == "PENDING" else utcnow() - timedelta(days=1)))
    with db.session() as s:
        ov = quality.override_rate(s, "homemonde")["inventory_ai"]
        assert ov == {"decided": 5, "accepted": 2, "changed": 1, "rejected": 1, "sent_back": 1, "rate": 0.6}
        h = health.employee_health(s, env.cfg, {"inventory_ai"})["inventory_ai"]
        assert h["status"] == "NEEDS_ATTENTION" and "changed or turned down 3 of its last 5" in " ".join(h["reasons"])


def test_a_wrong_answer_becomes_a_test_draft_that_replays_the_same_situation(env, client, monkeypatch):
    import yaml

    from workforce import db, quality
    from workforce.models import Notification
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    wid = _run_next(env)
    with db.session() as s:
        quality.review(s, env.cfg, wid, _owner(s), "WRONG", "the batch was released the same day")
        assert [n.title for n in s.execute(select(Notification).where(Notification.kind == "quality")).scalars()] == [f"Turn {wid} into a quality test"]
        assert [t["work_id"] for t in quality.needs_test(s, env.cfg)] == [wid]
    csrf = _login(client, "founder@homemonde.in")
    r = client.get(f"/api/work/{wid}/test-draft")
    assert r.status_code == 200 and "WRITE_THE_RIGHT_ANSWER" in r.text and "anon-" in r.text  # customer e-mails never leave
    assert "@example.com" not in r.text
    assert client.get("/api/usage").json()["needs_test"][0]["work_id"] == wid
    sc = yaml.safe_load(r.text)[0]
    assert sc["id"] == quality.scenario_id(wid) and sc["expect"]["work"] == [{"type": "STOCKOUT_RISK", "group": "HM-204"}]
    assert {"inventory", "order_lines", "production_batches", "purchase_orders", "supplier_scorecard"} <= set(sc["datasets"])
    # unfilled, the draft fails; once a person writes the right answer it replays the same situation
    monkeypatch.setattr(quality, "load_suite", lambda slug: [sc])
    fake = use_fake([lambda kw: [text(_answer([]))]] * 10)
    _, res = quality.run_suite("homemonde")
    assert res[0].status == "FAILED" and fake.calls  # unfilled answer fails
    assert any(c["check"].startswith("run 3 of 3:") for c in res[0].checks)  # inventory_ai can spend money: three runs
    rule = next(c for c in res[0].checks if c["check"] == "run 1 of 3: rules created the expected work")
    assert rule["result"] == "pass"
    with db.session() as s:
        assert quality.needs_test(s, env.cfg) == []  # now in the suite
    assert csrf


def test_employees_that_can_spend_money_are_tested_three_times(env, monkeypatch):
    from workforce import quality
    sc = {"id": "x", "expect": {"work": [{"type": "STOCKOUT_RISK", "group": "HM-204"}]}}
    assert quality._test_runs(env.cfg, sc) == 3  # inventory_ai can propose purchases
    assert quality._test_runs(env.cfg, {"id": "y", "expect": {"work": []}}) == 1
    assert quality._test_runs(env.cfg, {**sc, "runs": 5}) == 5
    calls = []

    def fake_run(cfg, scenario):
        calls.append(1)
        ok = len(calls) != 2  # the second of three runs gives a different answer
        return [{"check": "outcome", "result": "pass" if ok else "fail", "detail": ""}], "inventory_ai", (10, 5, 0.01)

    monkeypatch.setattr(quality, "load_suite", lambda slug: [sc])
    monkeypatch.setattr(quality, "_run_scenario", fake_run)
    _, res = quality.run_suite("homemonde")
    assert len(calls) == 3 and res[0].status == "FAILED"  # one inconsistent run fails the scenario
    assert [c["check"] for c in res[0].checks] == ["run 1 of 3: outcome", "run 2 of 3: outcome", "run 3 of 3: outcome"]
    assert res[0].input_tokens == 30 and res[0].cost_usd == 0.03


# ---------------------------------------------------------------- item 13: certainty from evidence, review when evidence is weak

def test_certainty_is_judged_from_the_evidence_and_can_only_go_down(env):
    from workforce import validation
    ev = {"E1": {"result": {"available": 320}, "freshness": "CURRENT", "name": "get_inventory_position", "datasets": ["inventory"]},
          "E2": {"result": {"avg": 95}, "freshness": "CURRENT", "name": "get_sales_velocity", "datasets": ["order_lines"]},
          "M1": {"result": {"inventory": 320, "orders": 300}, "freshness": "CURRENT", "name": "source_conflict", "datasets": ["inventory"]}}
    fact1 = {"kind": "FACT", "text": "320 units are available.", "refs": ["E1"]}
    fact2 = {"kind": "FACT", "text": "Demand is 95 a day.", "refs": ["E2"]}
    guess = {"kind": "INFERENCE", "text": "Stock will run out soon.", "refs": ["E1"]}
    assert validation.certainty("CONFIRMED", [fact1], ev, env.cfg)[0] == "CONFIRMED"
    assert validation.certainty("POSSIBLE", [fact1], ev, env.cfg)[0] == "POSSIBLE"  # never raised above the model's own view
    q, basis = validation.certainty("CONFIRMED", [fact1, guess], ev, env.cfg)
    assert q == "POSSIBLE" and any("only one kind of record" in b for b in basis)
    q, basis = validation.certainty("CONFIRMED", [fact1, fact2, guess], ev, env.cfg)
    assert q == "LIKELY" and basis[0].startswith("The facts come from 2 kinds of business records")
    assert validation.certainty("LIKELY", [{"kind": "FACT", "text": "x", "refs": ["M1"]}], ev, env.cfg)[0] == "DATA_CONFLICT"
    assert validation.certainty("CONFIRMED", [guess], ev, env.cfg)[0] == "INSUFFICIENT_EVIDENCE"


def test_urgent_finding_on_weak_evidence_goes_to_a_person_and_its_action_is_held(env):
    from workforce import db
    from workforce.models import Approval, Notification
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)

    def final(kw):  # leans on one kind of record and still claims confidence, with a purchase
        return [text(_answer([{"kind": "FACT", "text": "320 units are available and 40 are reserved.", "refs": ["E1"]},
                              {"kind": "INFERENCE", "text": "Stock will run out within days.", "refs": ["E1"]}], quality="CONFIRMED",
                             rec={"action": "create_purchase_request", "summary": "Buy fabric", "rationale": "low stock", "refs": ["E1"],
                                  "params": [{"name": "material_code", "value": "FAB-CT-02"}, {"name": "supplier", "value": "Bagru Cotton Mills"},
                                             {"name": "quantity", "value": "800"}, {"name": "unit", "value": "m"}, {"name": "unit_price", "value": "162"}]}))]

    use_fake([lambda kw: [tool_use("get_inventory_position", {"sku": "HM-204"}, 1)], final])
    wid = _run_next(env)
    it = _item(wid)
    f = it.finding
    assert f["evidence_quality"] == "POSSIBLE" and f["recommendation"] is None and f["recommendation_held"]["action"] == "create_purchase_request"
    assert it.status == "NEEDS_HUMAN_REVIEW" and it.outcome == "HUMAN_REVIEW_REQUIRED"
    assert any("urgent and the evidence is weak" in b for b in f["quality_basis"])
    with db.session() as s:
        assert s.execute(select(Approval)).first() is None  # nothing goes for approval on weak evidence
        assert s.execute(select(Notification).where(Notification.work_id == wid)).first() is not None


def test_well_corroborated_finding_still_goes_for_approval(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    it = _item(_run_next(env))
    assert it.finding["evidence_quality"] == "LIKELY" and it.finding["recommendation"]  # facts from 3+ kinds of record
    assert it.status == "NEEDS_HUMAN_REVIEW" and it.outcome != "HUMAN_REVIEW_REQUIRED"  # waiting for the approval decision


# ---------------------------------------------------------------- item 14: data problems are tasks with an owner

def _issues(s):
    from workforce.models import WorkItem
    return s.execute(select(WorkItem).where(WorkItem.type == "DATA_ISSUE").order_by(WorkItem.created_at)).scalars().all()


def test_a_failing_feed_becomes_one_owned_task_that_closes_itself_when_fixed(env):
    from workforce import data, db, work
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")  # no files yet: fails
        assert _issues(s) == []  # once is noise
        data.sync_source(s, env.cfg, "exports")
        data.sync_source(s, env.cfg, "exports")
        issues = _issues(s)
        assert len(issues) == 1  # repeated failures update the same task
        it = issues[0]
        assert it.status == "NEEDS_HUMAN_REVIEW" and it.priority == "HIGH" and it.employee == data.steward(env.cfg, "exports")
        assert it.employee != env.cfg.question_employee()  # lands with the area that depends on the data
        assert "3 times in a row" in it.finding["statements"][0]["text"] and it.finding["owner"]
        with pytest.raises(work.WorkError):
            work.resolve_review(s, env.cfg, it, _owner(s), "reinvestigate")  # nothing for the AI to investigate
    stockout_scenario(env.exports, env.cfg)
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
        it = _issues(s)[0]
        assert it.status == "COMPLETED" and it.outcome == "RESOLVED"


def test_a_refused_file_is_a_task_for_that_dataset(env):
    import csv

    from workforce import data, db
    stockout_scenario(env.exports, env.cfg)
    good = (env.exports / "inventory.csv").read_text()
    with open(env.exports / "inventory.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sku", "location", "product_name", "available", "reserved", "incoming"])
        w.writerows([["HM-1", "JAIPUR-WH", "x", "lots", "0", "0"], ["HM-2", "JAIPUR-WH", "y", "many", "0", "0"]])
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
        issues = _issues(s)
        assert len(issues) == 1 and issues[0].context == {"source": "exports", "dataset": "inventory"}
        assert "could not be read" in issues[0].finding["statements"][0]["text"]
    (env.exports / "inventory.csv").write_text(good)
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
        assert _issues(s)[0].outcome == "RESOLVED"


def test_controls_api_pauses_one_action_type_or_feed_owners_only(env, client):
    from workforce import db, security
    from workforce.models import User
    with db.session() as s:
        s.add(User(tenant_id="homemonde", email="wh@homemonde.in", name="Warehouse Head", role="manager", areas=["warehouse"],
                   password_hash=security.hash_password("correct horse battery")))
    h = {"X-CSRF-Token": _login(client, "wh@homemonde.in")}
    assert client.get("/api/controls").status_code == 403
    assert client.post("/api/controls", json={"kind": "source", "target": "exports", "paused": True, "reason": "broken"}, headers=h).status_code == 403
    h = {"X-CSRF-Token": _login(client, "founder@homemonde.in")}
    body = {"kind": "action", "target": "create_purchase_request", "paused": True}
    assert client.post("/api/controls", json=body, headers=h).status_code == 400  # a reason is required
    assert client.post("/api/controls", json={**body, "target": "nope", "reason": "audit"}, headers=h).status_code == 404
    assert client.post("/api/controls", json={**body, "kind": "employee", "reason": "audit"}, headers=h).status_code == 422
    assert client.post("/api/controls", json={**body, "reason": "supplier audit"}, headers=h).status_code == 200
    ctl = client.get("/api/controls").json()
    act = next(a for a in ctl["actions"] if a["id"] == "create_purchase_request")
    assert act["paused"] and act["reason"] == "supplier audit"
    assert next(x for x in ctl["sources"] if x["id"] == "exports")["owner"] == "ERP / IT administrator"
    assert client.post("/api/controls", json={**body, "paused": False}, headers=h).status_code == 200
    assert not next(a for a in client.get("/api/controls").json()["actions"] if a["id"] == "create_purchase_request")["paused"]
