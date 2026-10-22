"""Regressions for the defects confirmed by the real-user adversarial QA audit (QA_Audit_Stage1_Report.md)."""
import json
import os
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import stockout_scenario, write_csv

PASSWORD = "correct horse battery"


def _sync_and_rules(env, source="exports"):
    from workforce import data, db, rules
    with db.session() as s:
        data.sync_source(s, env.cfg, source)
    with db.session() as s:
        return rules.evaluate(s, env.cfg)


def _user(email, role="manager", areas=(), limit=None):
    from workforce import db, security
    from workforce.models import User
    with db.session() as s:
        u = User(tenant_id="homemonde", email=email, name=email.split("@")[0], role=role, areas=list(areas), approval_limit=limit,
                 password_hash=security.hash_password(PASSWORD))
        s.add(u)
        s.flush()
        return u.id


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf"]}


def _pending_purchase_request(env):
    """A pending create_purchase_request for 800 m at 162 (INR 129,600), as Inventory AI would propose it."""
    from workforce import actions, db
    from workforce.models import WorkItem
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    with db.session() as s:
        item = s.execute(select(WorkItem).where(WorkItem.type == "STOCKOUT_RISK")).scalar_one()
        ap = actions.propose(s, env.cfg, item, {"action": "create_purchase_request", "summary": "Buy fabric", "rationale": "r",
                                                "params": {"material_code": "FAB-CT-02", "quantity": 800, "unit": "m",
                                                           "unit_price": 162, "supplier": "Bagru Cotton Mills",
                                                           "needed_by": "2026-12-01"}, "refs": ["E1"]})
        return ap.id, item.id


# ---------------------------------------------------------------- SEC-1 approval limit

def test_a_limited_manager_cannot_lower_the_price_to_fit_their_limit(env):
    from workforce import actions, db
    from workforce.models import ActionRecord, User
    apid, _ = _pending_purchase_request(env)
    _user("buyer@x.in", areas=["procurement"], limit=100000)
    with db.session() as s:
        mgr = s.execute(select(User).where(User.email == "buyer@x.in")).scalar_one()
        with pytest.raises(actions.DecisionError) as e:
            actions.decide(s, env.cfg, apid, mgr, "MODIFIED", {"unit_price": 0.01})
        assert e.value.status == 400 and "unit_price" in str(e.value)
    with db.session() as s:
        assert s.execute(select(ActionRecord)).scalars().all() == []
    with db.session() as s:  # buying less, within the limit, is still allowed
        mgr = s.execute(select(User).where(User.email == "buyer@x.in")).scalar_one()
        actions.decide(s, env.cfg, apid, mgr, "MODIFIED", {"quantity": 600})


# ---------------------------------------------------------------- SEC-2 HR figures through colleague evidence

def _task_with_hr_evidence(env):
    from workforce import db, work
    from workforce.models import Evidence, Record, WorkItem
    write_csv(env.exports / "attendance.csv", [{"att_date": "2026-10-20", "employee_id": f"E{i}", "department": "stitching",
                                                "status": "absent" if i % 4 == 0 else "present"} for i in range(8)])
    from workforce import data
    with db.session() as s:
        data.sync_source(s, env.cfg, "hrms")
    with db.session() as s:
        rids = [r.id for r in s.execute(select(Record).where(Record.dataset == "attendance")).scalars()]
        item = work.create(s, env.cfg, employee="manufacturing_ai", type_="PRODUCTION_SHORTFALL", title="Production below plan · STITCH-2",
                           origin="rule")
        s.add_all([Evidence(tenant_id="homemonde", work_id=item.id, ref="E1", kind="tool", employee="manufacturing_ai", name="get_line_output",
                            args={}, result={"achievement": 0.7}, freshness="CURRENT", record_ids=[], record_count=0),
                   Evidence(tenant_id="homemonde", work_id=item.id, ref="E2", kind="tool", employee="hr_ai", name="get_absence_by_department",
                            args={}, result={"absence_rate_7d": 0.25}, freshness="CURRENT", record_ids=rids, record_count=len(rids))])
        item.status = "COMPLETED"
        item.finding = {"headline": "Stitching is short", "statements": [
            {"kind": "FACT", "text": "STITCH-2 made 70% of plan.", "refs": ["E1"]},
            {"kind": "FACT", "text": "Stitching absence was 25%.", "refs": ["E2"]}], "evidence_quality": "LIKELY"}
        return item.id


def test_hr_figures_reach_only_people_who_may_see_hr(env, client):
    wid = _task_with_hr_evidence(env)
    _user("factory@x.in", areas=["factory"])
    h = _login(client, "factory@x.in")
    d = client.get(f"/api/work/{wid}").json()
    blob = json.dumps(d)
    assert "0.25" not in blob and "25%" not in blob and "absence_rate_7d" not in blob
    e2 = next(e for e in d["evidence"] if e["ref"] == "E2")
    assert e2["withheld"] is True and "result" not in e2
    texts = [st["text"] for st in d["root"]["finding"]["statements"]]
    assert texts[0] == "STITCH-2 made 70% of plan." and "withheld" in texts[1].lower()
    assert client.get(f"/api/work/{wid}/evidence/E2/records", headers=h).status_code == 404
    assert client.get(f"/api/work/{wid}/evidence/E1/records", headers=h).status_code == 200
    _login(client, "founder@homemonde.in")  # owners still see everything
    assert "0.25" in json.dumps(client.get(f"/api/work/{wid}").json())


# ---------------------------------------------------------------- UX-1 managers' own questions

def test_a_manager_sees_the_answer_to_their_own_question_and_only_theirs(env, client):
    _user("buyer@x.in", areas=["procurement"])
    _user("other@x.in", areas=["procurement"])
    h = _login(client, "buyer@x.in")
    wid = client.post("/api/questions", json={"text": "Which purchase orders are late?"}, headers=h).json()["work_id"]
    assert client.get(f"/api/work/{wid}").status_code == 200
    assert wid in json.dumps(client.get("/api/work").json())
    _login(client, "other@x.in")
    assert client.get(f"/api/work/{wid}").status_code == 404


# ---------------------------------------------------------------- UX-2 decision counts

def test_home_lists_every_decision_the_top_bar_counts(env, client):
    _pending_purchase_request(env)  # proposed by Inventory AI, decided by procurement
    _user("buyer@x.in", areas=["procurement"], limit=200000)
    _login(client, "buyer@x.in")
    on_top = len(client.get("/api/state").json()["approvals"])
    on_home = len(client.get("/api/home").json()["decisions"])
    assert on_top == on_home == 1


# ---------------------------------------------------------------- PIPE-8 three-valued logic

@pytest.mark.parametrize("e,vals,want", [("a < 1 and b > 2", {"a": None, "b": 0}, False), ("a < 1 and b > 2", {"a": None, "b": 9}, None),
                                         ("x == 1 or y < 2", {"x": 1, "y": None}, True), ("x == 1 or y < 2", {"x": 0, "y": None}, None),
                                         ("a > 0 and b > 0", {"a": 1, "b": 1}, True), ("a > 0 or b > 0", {"a": 0, "b": 0}, False)])
def test_unknown_and_false_is_false(e, vals, want):
    from workforce import expr
    assert expr.evaluate(e, vals) is want


def test_an_ads_alert_closes_when_the_spend_stops(env):
    from workforce import data, db, rules
    from workforce.data import today
    from workforce.models import WorkItem
    t = today(env.cfg)

    def write(spend_now):
        write_csv(env.exports / "marketing_spend.csv", [
            {"spend_date": (t - timedelta(days=d)).isoformat(), "channel": "meta", "campaign": "c",
             "spend": (spend_now if d < 7 else 10000), "attributed_revenue": (spend_now if d < 7 else 40000)} for d in range(28)])
    write(5000)
    for _ in (1, 2):
        with db.session() as s:
            data.sync_source(s, env.cfg, "marketing")
        with db.session() as s:
            rules.evaluate(s, env.cfg)
        write(0)
    with db.session() as s:
        [it] = s.execute(select(WorkItem).where(WorkItem.type == "ROAS_DROP")).scalars().all()
        assert it.status == "COMPLETED" and it.outcome == "CONDITION_CLEARED"


# ---------------------------------------------------------------- PIPE-6 partial sync

def test_a_sync_that_fails_halfway_leaves_the_previous_data(env, monkeypatch):
    from workforce import data, db
    from workforce.metrics import MetricEngine
    stockout_scenario(env.exports, env.cfg)
    rows = list(__import__("csv").DictReader((env.exports / "order_lines.csv").open()))
    big = [{**rows[i % len(rows)], "order_id": f"B{i}"} for i in range(1200)]
    write_csv(env.exports / "order_lines.csv", big)
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
    with db.session() as s:
        before = MetricEngine(s, env.cfg).values("sales_7d")
    write_csv(env.exports / "order_lines.csv", [{**r, "net_amount": float(r["net_amount"]) * 2} for r in big])
    real, calls = data._upsert, []

    def flaky(db_, rows_):
        calls.extend(r["dataset"] for r in rows_[:1])
        if calls.count("order_lines") == 2:  # the second batch of 500 order lines
            raise RuntimeError("database went away")
        return real(db_, rows_)
    monkeypatch.setattr(data, "_upsert", flaky)
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
    with db.session() as s:
        assert MetricEngine(s, env.cfg).values("sales_7d") == before
        assert data.freshness(s, env.cfg, "order_lines")[0] == "STALE"


# ---------------------------------------------------------------- PIPE-2 / PIPE-3 snapshots

def test_a_stock_file_that_stopped_changing_is_out_of_date(env):
    from workforce import db
    from workforce.models import WorkItem
    stockout_scenario(env.exports, env.cfg)
    old = time.time() - 5 * 86400
    os.utime(env.exports / "inventory.csv", (old, old))
    _sync_and_rules(env)
    from workforce import data
    with db.session() as s:
        assert data.freshness(s, env.cfg, "inventory")[0] == "STALE"
        assert s.execute(select(WorkItem).where(WorkItem.type == "STOCKOUT_RISK")).scalars().all() == []
        issue = s.execute(select(WorkItem).where(WorkItem.type == "DATA_ISSUE", WorkItem.dedupe_key == "data:exports:inventory")).scalar_one()
        assert "has not changed since" in json.dumps(issue.finding)


def test_a_second_export_on_the_same_day_replaces_the_first(env):
    import csv
    from workforce import db
    from workforce.metrics import MetricEngine
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    inv = list(csv.DictReader((env.exports / "inventory.csv").open()))
    write_csv(env.exports / "inventory.csv", [r for r in inv if r["sku"] != "HM-118"])
    _sync_and_rules(env)
    with db.session() as s:
        e = MetricEngine(s, env.cfg)
        assert e.for_group("sellable_units", "sku", "HM-118") is None
        assert e.for_group("sellable_units", "sku", "HM-204") == 280


# ---------------------------------------------------------------- SEC-3 / SEC-4 personal data in messages

def test_personal_key_values_never_appear_in_data_messages(env):
    from workforce import data, db
    from workforce.models import Event, WorkItem
    write_csv(env.exports / "attendance.csv", [{"att_date": "2026-10-20", "employee_id": "EMP-SECRET-4711", "department": "qc", "status": s_}
                                               for s_ in ("present", "absent")] +
              [{"att_date": "2026-10-20", "employee_id": f"E{i}", "department": "qc", "status": "present"} for i in range(60)] +
              [{"att_date": "2026-10-20", "employee_id": "EMP-BAD-9", "department": "", "status": "present"}])
    with db.session() as s:
        data.sync_source(s, env.cfg, "hrms")
    with db.session() as s:
        text = json.dumps([(e.msg, e.payload) for e in s.execute(select(Event)).scalars()], default=str)
        text += json.dumps([i.finding for i in s.execute(select(WorkItem)).scalars()], default=str)
    assert "EMP-SECRET-4711" not in text and "EMP-BAD-9" not in text


def test_columns_the_pack_does_not_declare_are_not_shown(env, client):
    import csv
    from workforce import db
    from workforce.models import Evidence, Record, WorkItem
    from workforce import work
    stockout_scenario(env.exports, env.cfg)
    rows = list(csv.DictReader((env.exports / "order_lines.csv").open()))
    write_csv(env.exports / "order_lines.csv", [{**r, "customer_phone": "+91-9800000000"} for r in rows])
    _sync_and_rules(env)
    with db.session() as s:
        rid = s.execute(select(Record.id).where(Record.dataset == "order_lines")).scalars().first()
        item = s.execute(select(WorkItem).where(WorkItem.type == "STOCKOUT_RISK")).scalar_one()
        s.add(Evidence(tenant_id="homemonde", work_id=item.id, ref="E9", kind="tool", employee="inventory_ai", name="x", args={},
                       result={}, freshness="CURRENT", record_ids=[rid], record_count=1))
        wid = item.id
    _user("stock@x.in", areas=["warehouse"])
    _login(client, "stock@x.in")
    r = client.get(f"/api/work/{wid}/evidence/E9/records")
    assert r.status_code == 200 and "9800000000" not in r.text and "customer_phone" not in r.text


# ---------------------------------------------------------------- PIPE-7 duplicate-invoice speed

def test_duplicate_invoice_check_stays_fast_with_many_invoices(env):
    from workforce import db, rules
    from workforce.metrics import MetricEngine
    from workforce.data import today
    t = today(env.cfg)
    rows = [{"voucher_id": f"V{i}", "invoice_number": f"INV-{i}", "supplier": f"S{i % 40}", "po_number": "", "material_code": "",
             "quantity": "", "unit_price": "", "total_amount": 1000 + i * 7.31, "invoice_date": (t - timedelta(days=i % 300)).isoformat(),
             "status": "OPEN"} for i in range(8000)]
    rows.append({**rows[5], "voucher_id": "V-DUP", "invoice_number": "inv 5"})  # the same invoice entered twice
    write_csv(env.exports / "supplier_invoices.csv", rows)
    write_csv(env.exports / "goods_receipts.csv", [{"grn_number": "G1", "po_number": "P", "material_code": "", "quantity": 1,
                                                   "received_date": t.isoformat()}])
    from workforce import data
    with db.session() as s:
        data.sync_source(s, env.cfg, "payables")
    rule = next(r for r in env.cfg.rules if r.kind == "duplicate")
    with db.session() as s:
        t0 = time.perf_counter()
        made = rules._duplicates(s, env.cfg, rule, MetricEngine(s, env.cfg))
        took = time.perf_counter() - t0
    assert made == 1 and took < 3, took


# ---------------------------------------------------------------- SEC-5 login abuse

def test_repeated_failed_logins_from_one_address_are_slowed_down(env, client):
    for i in range(20):
        client.post("/api/auth/login", json={"email": f"nobody{i}@x.in", "password": "wrong password!!"})
    r = client.post("/api/auth/login", json={"email": "founder@homemonde.in", "password": PASSWORD})
    assert r.status_code == 429


def test_a_locked_account_takes_as_long_to_answer_as_any_other(env, monkeypatch):
    from workforce import db, security
    from workforce.models import User
    calls = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password", lambda p, h: calls.append(1) or real(p, h))
    _user("lock@x.in", role="owner")
    with db.session() as s:
        for _ in range(security.MAX_FAILED):
            security.authenticate(s, "lock@x.in", "wrong", "1.1.1.1")
    calls.clear()
    with db.session() as s:
        assert security.authenticate(s, "lock@x.in", PASSWORD, "1.1.1.1") is None
    assert calls == [1]  # the password is still hashed, so the reply time does not reveal the lock


# ---------------------------------------------------------------- AI-1 "nothing found" is not proof

def test_a_claim_resting_only_on_a_search_for_a_value_that_does_not_exist_is_not_a_fact(env):
    from workforce import validation
    ev = {"E1": {"name": "get_purchase_orders", "freshness": "CURRENT", "datasets": ["purchase_orders"],
                 "result": {"rows": [], "total_matches": 0, "unmatched_filters": ["material_code"]}}}
    finding = {"outcome": "FINDING", "headline": "There are no late purchase orders.", "evidence_quality": "CONFIRMED",
               "statements": [{"kind": "FACT", "text": "No purchase orders are late.", "refs": ["E1"]}], "unknowns": [],
               "impact": "", "recommendation": None}
    cleaned, problems = validation.check(finding, ev, env.cfg, "executive_ai")
    assert cleaned["evidence_quality"] == "INSUFFICIENT_EVIDENCE" and cleaned["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert any("found nothing" in p for p in problems)
    assert any("does not show" in u for u in cleaned["unknowns"])
    real = {"E1": {**ev["E1"], "result": {"rows": [], "total_matches": 0}}}  # a real value with no rows is a fact
    assert validation.check(finding, real, env.cfg, "executive_ai")[0]["evidence_quality"] == "CONFIRMED"


def test_a_search_that_matches_nothing_says_whether_the_value_exists(env):
    from workforce import db, tools
    from workforce.metrics import MetricEngine
    from workforce.models import WorkItem
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    with db.session() as s:
        item = s.execute(select(WorkItem)).scalars().first()
        ctx = tools.ToolContext(db=s, cfg=env.cfg, root=item, engine=MetricEngine(s, env.cfg), employee="procurement_ai")
        made_up = tools._run_config_tool(ctx, "get_purchase_orders", env.cfg.tools["get_purchase_orders"], {"material_code": "ALL"})[0]
        real = tools._run_config_tool(ctx, "get_purchase_orders", env.cfg.tools["get_purchase_orders"], {"material_code": "FAB-CT-02"})[0]
    assert made_up["total_matches"] == 0 and made_up["unmatched_filters"] == ["material_code"] and "does not show" in made_up["note"]
    assert real["total_matches"] == 1 and "unmatched_filters" not in real


def test_late_orders_can_be_listed_without_guessing_a_filter(env):
    from workforce import db, tools
    from workforce.metrics import MetricEngine
    from workforce.models import WorkItem
    from workforce.data import today
    stockout_scenario(env.exports, env.cfg)
    write_csv(env.exports / "purchase_orders.csv", [
        {"po_number": "PO-7731", "material_code": "FAB-CT-02", "supplier": "Sanganer Textiles", "quantity": 2000, "unit": "m",
         "due_date": (today(env.cfg) - timedelta(days=4)).isoformat(), "revised_eta": "", "status": "LATE"},
        {"po_number": "PO-7710", "material_code": "FAB-CT-01", "supplier": "Sanganer Textiles", "quantity": 1500, "unit": "m",
         "due_date": (today(env.cfg) + timedelta(days=3)).isoformat(), "revised_eta": "", "status": "OPEN"}])
    _sync_and_rules(env)
    assert "get_purchase_order_timing" in env.cfg.employees["procurement_ai"].tools
    with db.session() as s:
        item = s.execute(select(WorkItem)).scalars().first()
        ctx = tools.ToolContext(db=s, cfg=env.cfg, root=item, engine=MetricEngine(s, env.cfg), employee="procurement_ai")
        out = tools._run_config_tool(ctx, "get_purchase_order_timing", env.cfg.tools["get_purchase_order_timing"], {})[0]
    rows = out["metrics"]["po_days_overdue"]["rows"]
    assert rows[0] == {"po_number": "PO-7731", "value": 4.0}


def test_a_question_is_told_about_the_open_alerts_its_asker_may_see(env, client):
    from workforce import db
    from workforce.models import WorkItem
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    _user("buyer@x.in", areas=["procurement"])
    h = _login(client, "buyer@x.in")
    wid = client.post("/api/questions", json={"text": "Anything urgent?"}, headers=h).json()["work_id"]
    with db.session() as s:
        alerts = s.get(WorkItem, ("homemonde", wid)).context.get("open_alerts")
    assert alerts == []  # the stockout belongs to Inventory AI, outside procurement
    h = _login(client, "founder@homemonde.in")
    wid = client.post("/api/questions", json={"text": "Anything urgent?"}, headers=h).json()["work_id"]
    with db.session() as s:
        alerts = s.get(WorkItem, ("homemonde", wid)).context.get("open_alerts")
    assert [a["title"] for a in alerts] == ["Stockout risk · HM-204"]
