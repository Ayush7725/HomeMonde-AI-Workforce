import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import last_tool_results, stockout_scenario, text, tool_use, use_fake, write_csv


def _sync_and_rules(env):
    from workforce import data, db, rules
    with db.session() as s:
        res = data.sync_source(s, env.cfg, "exports")
    with db.session() as s:
        created = rules.evaluate(s, env.cfg)
    return res, created


def _run_next(env):
    from workforce import db, runtime, work
    with db.session() as s:
        item = work.claim(s, ["homemonde"], 600)
        wid = item.id if item else None
    assert wid, "expected a queued work item"
    with db.session() as s:
        from workforce.models import WorkItem
        runtime.run(s, env.cfg, s.get(WorkItem, ("homemonde", wid)))
    return wid


def _item(wid):
    from workforce import db
    from workforce.models import WorkItem
    with db.session() as s:
        it = s.get(WorkItem, ("homemonde", wid))
        s.expunge(it)
        return it


def _seeded_refs(kwargs) -> dict:
    brief = json.loads(kwargs["messages"][0]["content"])
    return {e["metric"]: e["ref"] for e in brief.get("evidence_already_gathered", []) if "metric" in e}


# ---------------------------------------------------------------- data + rules

def test_rule_fires_only_for_at_risk_sku_and_metrics_are_exact(env):
    stockout_scenario(env.exports, env.cfg)
    res, created = _sync_and_rules(env)
    assert res["inventory"] == 2 and created == 1
    from workforce import db
    from workforce.metrics import MetricEngine
    from workforce.models import WorkItem
    with db.session() as s:
        eng = MetricEngine(s, env.cfg)
        assert eng.for_group("sellable_units", "sku", "HM-204") == 280
        assert eng.for_group("demand_7d", "sku", "HM-204") == 95
        assert eng.for_group("demand_30d", "sku", "HM-204") == 71
        assert eng.for_group("stock_coverage_days", "sku", "HM-204") == round(280 / 95, 4)
        items = s.execute(select(WorkItem)).scalars().all()
        assert [(i.type, i.employee, i.context["group"]) for i in items] == [("STOCKOUT_RISK", "inventory_ai", "HM-204")]
    # dedupe: a second pass inside the cooldown creates nothing
    assert _sync_and_rules(env)[1] == 0


def test_invalid_batch_is_rejected_whole_and_previous_data_kept(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    write_csv(env.exports / "inventory.csv", [{"sku": "HM-204", "location": "JAIPUR-WH", "product_name": "x", "available": "lots", "reserved": 0, "incoming": 0}])
    from workforce import data, db
    from workforce.models import Event
    with db.session() as s:
        assert data.sync_source(s, env.cfg, "exports")["inventory"] == 0
        assert s.execute(select(Event).where(Event.type == "DATA_REJECTED")).scalars().first() is not None
        assert {r["available"] for r in data.rows(s, env.cfg, "inventory")} == {320, 5000}


def test_rules_do_not_fire_on_stale_data(env):
    stockout_scenario(env.exports, env.cfg)
    from workforce import data, db, rules
    from workforce.db import utcnow
    from workforce.models import SourceState
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
        s.get(SourceState, ("homemonde", "exports")).last_success = utcnow() - timedelta(hours=5)
    with db.session() as s:
        assert data.freshness(s, env.cfg, "inventory")[0] == "STALE"
        assert rules.evaluate(s, env.cfg) == 0


def test_missing_source_is_a_visible_failure(env):
    from workforce import data, db
    from workforce.models import Event, SourceState
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
        st = s.get(SourceState, ("homemonde", "exports"))
        assert st.status == "FAILED" and st.consecutive_failures == 1
        ev = s.execute(select(Event).where(Event.type == "DATA_SOURCE_FAILURE")).scalar_one()
        assert "inventory.csv" in ev.msg


# ---------------------------------------------------------------- AI employees

def test_without_ai_the_employee_still_reports_verified_facts(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    wid = _run_next(env)
    it = _item(wid)
    assert it.status == "COMPLETED" and it.outcome == "FINDING"
    f = it.finding
    assert f["ai"] is False and f["evidence_quality"] == "CONFIRMED" and f["recommendation"] is None
    assert all(s["kind"] == "FACT" and s["refs"] for s in f["statements"])
    assert any("no credentials configured for anthropic" in u for u in f["unknowns"])


def _stockout_script(final_overrides=None, repair=None):
    state = {}

    def s1(kw):
        state["m"] = _seeded_refs(kw)
        return [tool_use("get_inventory_position", {"sku": "HM-204"}, 1)]

    def s2(kw):
        assert last_tool_results(kw)["tu_1_get_inventory_position"]["ref"] == "E1"
        return [tool_use("ask_sales_ai__get_sales_velocity", {"sku": "HM-204"}, 2), tool_use("get_production_schedule", {"sku": "HM-204"}, 2)]

    def s3(kw):
        return [tool_use("ask_procurement_ai__get_purchase_orders", {"material_code": "FAB-CT-02"}, 3),
                tool_use("ask_procurement_ai__get_supplier_performance", {"material_code": "FAB-CT-02"}, 3)]

    def s4(kw):
        return [tool_use("calculate", {"expression": "(a - b) / b * 100", "label": "Demand vs 30-day average (%)",
                                       "inputs": [{"name": "a", "value": 95, "ref": "E2"}, {"name": "b", "value": 71, "ref": "E2"}]}, 4)]

    def final(kw):
        assert last_tool_results(kw)["tu_4_calculate"]["value"] == pytest.approx(33.8028, abs=1e-3)
        m = state["m"]
        out = {
            "outcome": "FINDING", "headline": "HM-204 is likely to stock out before batch PB-118 can replenish it",
            "statements": [
                {"kind": "FACT", "text": "Stock coverage is 2.9474 days with 280 units sellable.", "refs": [m["stock_coverage_days"], m["sellable_units"]]},
                {"kind": "FACT", "text": "Average daily demand is 95 units over 7 days and 71 over 30 days.", "refs": ["E2"]},
                {"kind": "OBSERVATION", "text": "Demand is 33.8% above the 30-day average.", "refs": ["C1"]},
                {"kind": "FACT", "text": "Batch PB-118 for 1500 units is blocked until 800 m of fabric FAB-CT-02 arrives.", "refs": ["E3"]},
                {"kind": "INFERENCE", "text": "Stock will likely run out before PB-118 can finish.", "refs": [m["stock_coverage_days"], "E3", "E4"]},
                {"kind": "HYPOTHESIS", "text": "The demand rise may come from a marketplace promotion; not checked.", "refs": []},
            ],
            "evidence_quality": "LIKELY", "impact": "HM-204 sales are at risk until replenishment.",
            "unknowns": ["Campaign data was not available."],
            "recommendation": {"action": "create_purchase_request", "summary": "Buy 800 m of FAB-CT-02 from Bagru Cotton Mills",
                               "rationale": "Bagru delivers in 2 days; PO-7731 is late.", "refs": ["E3", "E5"],
                               "params": [{"name": "material_code", "value": "FAB-CT-02"}, {"name": "supplier", "value": "Bagru Cotton Mills"},
                                          {"name": "quantity", "value": "800"}, {"name": "unit", "value": "m"}, {"name": "unit_price", "value": "162"}]},
        }
        out.update(final_overrides or {})
        return [text(out)]

    script = [s1, s2, s3, s4, final]
    if repair:
        script.append(repair)
    return script


def test_full_investigation_approval_and_action(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    fake = use_fake(_stockout_script())
    wid = _run_next(env)
    it = _item(wid)
    assert it.status == "NEEDS_HUMAN_REVIEW", it.finding
    f = it.finding
    assert f["evidence_quality"] == "LIKELY" and len(f["statements"]) == 6 and "validation_notes" not in f
    # token discipline: stable system prompt + tool list on every call, effort set, prompt caching on, refusal fallback on
    systems = {c["system"][0]["text"] for c in fake.calls}
    assert len(systems) == 1 and all(c["tools"] == fake.calls[0]["tools"] for c in fake.calls)
    assert fake.calls[0]["output_config"]["effort"] == "medium" and fake.calls[0]["cache_control"] == {"type": "ephemeral"}
    assert fake.calls[0]["fallbacks"] == "default"
    assert "customer_email" not in json.dumps([c["messages"] for c in fake.calls], default=str)

    from workforce import actions, db, events, security
    from workforce.models import ActionRecord, Approval, User, WorkItem
    with db.session() as s:
        children = s.execute(select(WorkItem).where(WorkItem.parent_id == wid)).scalars().all()
        assert sorted(c.employee for c in children) == ["procurement_ai", "procurement_ai", "sales_ai"]
        assert all(c.status == "COMPLETED" for c in children)
        ap = s.execute(select(Approval).where(Approval.work_id == wid)).scalar_one()
        assert ap.amount == 129600 and ap.status == "PENDING"
        owner = User(tenant_id="homemonde", email="o@x.in", name="Founder", role="owner", password_hash=security.hash_password("correct horse battery"))
        mgr = User(tenant_id="homemonde", email="m@x.in", name="Ops", role="manager", areas=["procurement"], approval_limit=100000,
                   password_hash=security.hash_password("correct horse battery"))
        s.add_all([owner, mgr])
        s.flush()
        with pytest.raises(actions.DecisionError) as e:
            actions.decide(s, env.cfg, ap.id, mgr, "APPROVED")
        assert e.value.status == 403
        apid = ap.id
    with db.session() as s:  # a manager may approve a smaller, modified quantity within their limit
        mgr = s.execute(select(User).where(User.email == "m@x.in")).scalar_one()
        actions.decide(s, env.cfg, apid, mgr, "MODIFIED", {"quantity": 600})
    with db.session() as s:
        ap = s.get(Approval, ("homemonde", apid))
        assert ap.status == "MODIFIED" and ap.final_params["quantity"] == 600
        rec = s.execute(select(ActionRecord)).scalar_one()
        assert rec.status == "VERIFIED" and rec.params["quantity"] == 600
        assert s.get(WorkItem, ("homemonde", wid)).outcome == "ACTION_TAKEN"
        with pytest.raises(actions.DecisionError):
            actions.decide(s, env.cfg, apid, mgr, "APPROVED")  # decisions are final
        assert events.verify_chain(s, "homemonde") == (True, None)


def test_ungrounded_numbers_are_removed_not_reported(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    bad = {"statements": [{"kind": "FACT", "text": "Sellable stock is 999 units.", "refs": ["E1"]},
                          {"kind": "FACT", "text": "Average daily demand is 95 units over 7 days.", "refs": ["E2"]},
                          {"kind": "FACT", "text": "Global demand for cushions rose 12% this year.", "refs": []}],
           "recommendation": {"action": "none", "summary": "", "rationale": "", "params": [], "refs": []}}
    script = _stockout_script(bad, repair=lambda kw: [text({**json.loads(kw["messages"][-2]["content"][0].text), **bad})])
    use_fake(script)
    wid = _run_next(env)
    f = _item(wid).finding
    texts = [s["text"] for s in f["statements"]]
    assert texts == ["Average daily demand is 95 units over 7 days."]
    assert any("999" in n for n in f["validation_notes"]) and any("no evidence ref" in n for n in f["validation_notes"])
    assert _item(wid).status == "COMPLETED"


def test_ai_cannot_use_tools_outside_its_role_or_invent_calc_inputs(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    seen = {}

    def s1(kw):
        return [tool_use("get_purchase_orders", {"material_code": "FAB-CT-02"}, 1),
                tool_use("calculate", {"expression": "a * 2", "label": "x", "inputs": [{"name": "a", "value": 12345, "ref": "E9"}]}, 1)]

    def s2(kw):
        seen.update(last_tool_results(kw))
        return [text({"outcome": "INSUFFICIENT_EVIDENCE", "headline": "", "statements": [], "evidence_quality": "INSUFFICIENT_EVIDENCE",
                      "impact": "", "unknowns": [], "recommendation": {"action": "none", "summary": "", "rationale": "", "params": [], "refs": []}})]

    use_fake([s1, s2])
    wid = _run_next(env)
    assert "not allowed" in seen["tu_1_get_purchase_orders"]
    assert "unknown ref E9" in seen["tu_1_calculate"]
    it = _item(wid)  # an urgent item the AI could not conclude goes to a person
    assert it.finding["outcome"] == "INSUFFICIENT_EVIDENCE" and it.outcome == "HUMAN_REVIEW_REQUIRED"


def test_transient_api_failure_schedules_a_retry(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    import anthropic
    import httpx2

    def boom(kw):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    use_fake([boom])
    wid = _run_next(env)
    it = _item(wid)
    assert it.status == "NEEDS_RETRY" and it.next_attempt_at is not None and "connection" in it.error


# ---------------------------------------------------------------- API, security, scope

def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


def test_api_requires_login_and_csrf_and_answers_out_of_scope_questions(env, client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/auth/login", json={"email": "founder@homemonde.in", "password": "wrong password!!"}).status_code == 401
    csrf = _login(client, "founder@homemonde.in")
    boot = client.get("/api/bootstrap").json()
    assert boot["tenant"]["name"] == "HomeMonde" and "approvals" in boot["layout"]["areas"]
    assert client.post("/api/questions", json={"text": "Who is the President of India?"}).status_code == 403  # no CSRF header
    r = client.post("/api/questions", json={"text": "Who is the President of India?"}, headers={"X-CSRF-Token": csrf})
    wid = r.json()["work_id"]
    use_fake([lambda kw: [text({"outcome": "OUT_OF_SCOPE", "headline": "", "statements": [], "evidence_quality": "INSUFFICIENT_EVIDENCE",
                                "impact": "", "unknowns": [], "recommendation": {"action": "none", "summary": "", "rationale": "", "params": [], "refs": []}})]])
    assert _run_next(env) == wid
    detail = client.get(f"/api/work/{wid}").json()
    assert detail["item"]["outcome"] == "OUT_OF_SCOPE" and detail["item"]["finding"]["statements"] == []
    assert detail["item"]["finding"]["headline"] == "Outside this workforce's business scope"
    assert client.get("/api/audit/verify").json()["intact"] is True


def test_manager_sees_only_their_area(env, client):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    _login(client, "sales.head@homemonde.in")
    st = client.get("/api/state").json()
    assert set(st["employees"]) == {"sales_ai"} and st["active"] == []
    wid = next(iter([e for e in client.get("/api/events").json()["events"] if e.get("work_id")]), {"work_id": "INV-2026-00001"})["work_id"]
    assert client.get(f"/api/work/{wid}").status_code == 404
    assert client.get("/api/audit/verify").status_code == 403


def test_login_lockout_after_repeated_failures(env, client):
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "founder@homemonde.in", "password": "nope nope nope"})
    r = client.post("/api/auth/login", json={"email": "founder@homemonde.in", "password": "correct horse battery"})
    assert r.status_code == 401


# ---------------------------------------------------------------- platform guarantees

def test_expressions_cannot_execute_code():
    from workforce import expr
    for bad in ["__import__('os')", "a.b", "[x for x in y]", "open('f')", "a[0]", "'text'"]:
        with pytest.raises(expr.ExprError):
            expr.parse(bad)
    assert expr.evaluate("a / b", {"a": 1, "b": 0}) is None
    assert expr.evaluate("x > 2 and y < 1", {"x": 3, "y": 0}) is True


def test_audit_chain_detects_tampering(env):
    from workforce import db, events
    from workforce.models import Event
    with db.session() as s:
        events.emit(s, "homemonde", "LOGIN", "a")
        events.emit(s, "homemonde", "LOGIN", "b")
    with db.session() as s:
        s.execute(select(Event).where(Event.seq == 1)).scalar_one().msg = "edited"
    with db.session() as s:
        assert events.verify_chain(s, "homemonde") == (False, 1)


def test_conflicting_sources_create_a_data_conflict_item(env, tmp_path, monkeypatch):
    """A second company with different data, rules and layout runs on the same code (onboarding by config)."""
    import shutil

    import yaml
    from workforce import data, db, rules, tenancy
    pack = yaml.safe_load(tenancy.pack_path("homemonde").read_text())
    pack.update(id="acme", name="Acme Furniture", business="Acme makes office furniture.")
    pack["sources"] = {"files": {"type": "csv", "location": str(tmp_path / "acme"), "schedule_minutes": 60}}
    pack["datasets"] = {
        "wms": {"label": "WMS stock", "source": "files", "extract": "wms.csv", "mode": "snapshot", "key": ["sku"],
                "fields": {"sku": {"required": True}, "on_hand": {"type": "integer"}}},
        "shop": {"label": "Shop stock", "source": "files", "extract": "shop.csv", "mode": "snapshot", "key": ["sku"],
                 "fields": {"sku": {"required": True}, "stock": {"type": "integer"}}}}
    pack["metrics"], pack["tools"], pack["actions"], pack["home"] = {}, {}, {}, {"kpis": []}
    pack["rules"] = [{"id": "stock_conflict", "kind": "conflict", "work_type": "DATA_CONFLICT", "employee": "inventory_ai", "priority": "HIGH",
                      "title": "Stock sources disagree · {sku}", "conflict": {"dataset": "wms", "other": "shop", "match_on": "sku", "field": "on_hand", "other_field": "stock"}}]
    for e in pack["employees"].values():
        e.update(tools=[], can_ask=[], actions=[])
    for prop in pack["layout"].get("props", []):
        prop.pop("dataset", None)  # acme has no shipments data
    tdir = tmp_path / "tenants"
    shutil.copytree(tenancy.pack_path("homemonde").parent.parent / "homemonde", tdir / "homemonde")
    (tdir / "acme").mkdir()
    (tdir / "acme" / "tenant.yaml").write_text(yaml.safe_dump(pack))
    (tmp_path / "acme").mkdir()
    write_csv(tmp_path / "acme" / "wms.csv", [{"sku": "DESK-1", "on_hand": 1200}, {"sku": "CHAIR-2", "on_hand": 40}])
    write_csv(tmp_path / "acme" / "shop.csv", [{"sku": "DESK-1", "stock": 1050}, {"sku": "CHAIR-2", "stock": 40}])
    monkeypatch.setenv("WORKFORCE_TENANTS_DIR", str(tdir))
    monkeypatch.setenv("WORKFORCE_TENANTS", "homemonde,acme")
    from workforce import settings
    settings.get_settings.cache_clear()
    tenancy.get.cache_clear()
    with db.session() as s:
        acme = tenancy.register_all(s)["acme"]
        data.sync_source(s, acme, "files")
    with db.session() as s:
        assert rules.evaluate(s, acme) == 1
        from workforce.models import WorkItem
        it = s.execute(select(WorkItem).where(WorkItem.tenant_id == "acme")).scalar_one()
        assert it.type == "DATA_CONFLICT" and it.context["conflict"] == {"wms": 1200, "shop": 1050, "field": "on_hand", "tolerance": 0}
    from workforce import runtime
    with db.session() as s:
        from workforce.models import WorkItem
        it = s.execute(select(WorkItem).where(WorkItem.tenant_id == "acme")).scalar_one()
        runtime.run(s, acme, it)
        assert it.finding["outcome"] == "DATA_CONFLICT" and it.finding["evidence_quality"] == "DATA_CONFLICT"
        assert s.execute(select(WorkItem).where(WorkItem.tenant_id == "homemonde")).first() is None  # tenants never mix
