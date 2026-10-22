"""Purchase-order tracking: overdue and slipped orders are found by rules, investigated against the production they
hold up, and chased with a supplier follow-up that the buyer approves word for word."""
from datetime import timedelta

from sqlalchemy import select

from conftest import last_tool_results, stockout_scenario, text, tool_use, use_fake, write_csv
from test_workforce import _item, _login, _run_next, _seeded_refs, _sync_and_rules


def _orders(env, rows):
    from workforce.data import today
    t = today(env.cfg)
    stockout_scenario(env.exports, env.cfg)
    write_csv(env.exports / "purchase_orders.csv", [
        {"po_number": po, "material_code": m, "supplier": s, "quantity": q, "unit": "m",
         "due_date": (t + timedelta(days=due)).isoformat(), "revised_eta": (t + timedelta(days=eta)).isoformat() if eta is not None else "",
         "status": st} for po, m, s, q, due, eta, st in rows])
    return _sync_and_rules(env)


def _rule_items():
    from workforce import db
    from workforce.models import WorkItem
    with db.session() as s:
        items = s.execute(select(WorkItem).where(WorkItem.rule_id.in_(["po_overdue", "po_eta_slipped"]))).scalars().all()
        return sorted((i.type, i.context.get("group"), i.context.get("evidence_pack")) for i in items)


def test_overdue_and_slipped_orders_are_found_and_received_ones_are_not(env):
    _orders(env, [("PO-7731", "FAB-CT-02", "Sanganer Textiles", 2000, -4, 4, "LATE"),     # 4 days overdue
                  ("PO-7740", "FAB-CT-01", "Sanganer Textiles", 1500, 6, 11, "OPEN"),     # not due yet, moved 5 days later
                  ("PO-7741", "FAB-LN-03", "Rajasthan Linen Co", 900, 8, 9, "OPEN"),      # moved 1 day: below the threshold
                  ("PO-7700", "FAB-CT-04", "Bagru Cotton Mills", 1200, -20, None, "RECEIVED")])  # done: never chased
    items = _rule_items()
    assert [(t, g) for t, g, _ in items] == [("PO_ETA_SLIPPED", "PO-7740"), ("PO_OVERDUE", "PO-7731")]
    overdue = next(p for t, _, p in items if t == "PO_OVERDUE")
    assert overdue["po_days_overdue"]["value"] == 4 and overdue["po_eta_slip_days"]["value"] == 8 and overdue["po_quantity"]["value"] == 2000


def _followup_script(message: str):
    state = {}

    def s1(kw):
        state["m"] = _seeded_refs(kw)
        assert set(state["m"]) == {"po_days_overdue", "po_eta_slip_days", "po_quantity"}
        return [tool_use("get_purchase_order", {"po_number": "PO-7731"}, 1)]

    def s2(kw):
        assert last_tool_results(kw)["tu_1_get_purchase_order"]["result"]["rows"][0]["supplier"] == "Sanganer Textiles"
        return [tool_use("ask_manufacturing_ai__get_batches_for_material", {"material_code": "FAB-CT-02"}, 2)]

    def final(kw):
        results = last_tool_results(kw)  # empty on a repair turn, which answers the validator instead
        if results:
            assert results["tu_2_ask_manufacturing_ai__get_batches_for_material"]["result"]["rows"][0]["batch_id"] == "PB-118"
        m = state["m"]
        return [text({
            "outcome": "FINDING", "headline": "Fabric order PO-7731 is late and is holding up batch PB-118",
            "statements": [
                {"kind": "FACT", "text": "PO-7731 is 4 days past its promised date.", "refs": [m["po_days_overdue"]]},
                {"kind": "FACT", "text": "Batch PB-118 for 1500 units is blocked waiting for this fabric.", "refs": ["E2"]},
            ],
            "evidence_quality": "LIKELY", "impact": "HM-204 production cannot start until the fabric arrives.", "unknowns": [],
            "recommendation": {"action": "draft_supplier_followup", "summary": "Ask Sanganer Textiles for a firm delivery date",
                               "rationale": "The order is late and blocks production.", "refs": ["E1", "E2"],
                               "params": [{"name": "supplier", "value": "Sanganer Textiles"}, {"name": "po_number", "value": "PO-7731"},
                                          {"name": "subject", "value": "PO-7731: please confirm the delivery date"},
                                          {"name": "message", "value": message}]},
        })]

    return [s1, s2, final]


def test_a_late_order_is_investigated_and_the_buyer_approves_the_follow_up(env, client):
    from workforce import db, security
    from workforce.models import ActionRecord, User
    _orders(env, [("PO-7731", "FAB-CT-02", "Sanganer Textiles", 2000, -4, 4, "LATE")])
    use_fake(_followup_script("Our order PO-7731 for 2000 m of FAB-CT-02 is past its due date and is holding up production. "
                              "Please confirm a firm delivery date and send the shipping documents."))
    with db.session() as s:  # the stockout alert runs first; this test is about the order
        from workforce import work
        from workforce.models import WorkItem
        stock = s.execute(select(WorkItem).where(WorkItem.rule_id == "stockout_risk")).scalar_one()
        work.transition(s, env.cfg, stock, "COMPLETED", "not part of this test", outcome="STOPPED")
    wid = _run_next(env)
    it = _item(wid)
    assert it.type == "PO_OVERDUE" and it.status == "NEEDS_HUMAN_REVIEW" and "validation_notes" not in it.finding
    with db.session() as s:
        s.add(User(tenant_id="homemonde", email="buyer@homemonde.in", name="Buyer", role="manager", areas=["procurement"],
                   password_hash=security.hash_password("correct horse battery")))
    csrf = _login(client, "buyer@homemonde.in")
    ap = client.get("/api/state").json()["approvals"][0]
    assert ap["action"] == "draft_supplier_followup" and "2000 m" in ap["params"]["message"]
    r = client.post(f"/api/approvals/{ap['id']}/decision", headers={"X-CSRF-Token": csrf},
                    json={"decision": "MODIFIED", "params": {"message": ap["params"]["message"] + " Thank you."}})
    assert r.status_code == 200
    with db.session() as s:
        rec = s.execute(select(ActionRecord)).scalar_one()
        assert rec.action == "draft_supplier_followup" and rec.params["message"].endswith("Thank you.") and rec.status == "VERIFIED"


def test_a_follow_up_that_invents_a_number_never_reaches_the_buyer(env):
    _orders(env, [("PO-7731", "FAB-CT-02", "Sanganer Textiles", 2000, -4, 4, "LATE")])
    from workforce import db, work
    from workforce.models import Approval, WorkItem
    with db.session() as s:
        stock = s.execute(select(WorkItem).where(WorkItem.rule_id == "stockout_risk")).scalar_one()
        work.transition(s, env.cfg, stock, "COMPLETED", "not part of this test", outcome="STOPPED")
    bad = "Please deliver the 2500 m of fabric on PO-7731 now."  # 2500 is not in any evidence
    script = _followup_script(bad)
    script.append(script[-1])  # the repair turn repeats the same mistake
    use_fake(script)
    wid = _run_next(env)
    it = _item(wid)
    assert it.finding["recommendation"] is None and any("2500" in n for n in it.finding["validation_notes"])
    with db.session() as s:
        assert s.execute(select(Approval)).first() is None


def test_an_approved_follow_up_can_be_sent_by_email_and_never_without_a_recipient(env, monkeypatch):
    import smtplib

    import pytest

    from workforce import actions, db, work
    from workforce.models import ActionRecord, Approval, User, WorkItem
    from workforce.tenancy import EmailConfig, ParamSpec
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host, self.port = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            sent.append("tls")

        def login(self, user, password):
            sent.append(("login", user, password))

        def send_message(self, msg):
            sent.append(msg)
            return {}

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("TEST_SMTP_URL", "smtp://buyer%40homemonde.in:s3cret@mail.example.com:587")
    act = env.cfg.actions["draft_supplier_followup"]
    act = act.model_copy(update={"handler": "email", "params": {**act.params, "to": ParamSpec()},
                                 "email": EmailConfig(smtp_url="${TEST_SMTP_URL}", sender="buyer@homemonde.in")})
    cfg = env.cfg.model_copy(update={"actions": {**env.cfg.actions, "draft_supplier_followup": act}})
    _orders(env, [("PO-7731", "FAB-CT-02", "Sanganer Textiles", 2000, -4, 4, "LATE")])
    with db.session() as s:
        stock = s.execute(select(WorkItem).where(WorkItem.rule_id == "stockout_risk")).scalar_one()
        work.transition(s, cfg, stock, "COMPLETED", "not part of this test", outcome="STOPPED")
    use_fake(_followup_script("Our order PO-7731 for 2000 m is past its due date. Please confirm a firm delivery date."))
    from workforce import runtime
    with db.session() as s:
        item = work.claim(s, ["homemonde"], 600)
        wid = item.id
    with db.session() as s:
        runtime.run(s, cfg, s.get(WorkItem, ("homemonde", wid)))
    with db.session() as s:
        owner = User(tenant_id="homemonde", email="o@x.in", name="Founder", role="owner", password_hash="x")
        s.add(owner)
        s.flush()
        apid = s.execute(select(Approval)).scalar_one().id
        with pytest.raises(actions.DecisionError, match="email address"):
            actions.decide(s, cfg, apid, owner, "APPROVED")  # the AI cannot know the address: a person adds it
        assert sent == []
        actions.decide(s, cfg, apid, owner, "MODIFIED", {"to": "sales@sanganer.example"})
    msg = sent[-1]
    assert sent[:2] == ["tls", ("login", "buyer@homemonde.in", "s3cret")]
    assert msg["To"] == "sales@sanganer.example" and msg["Subject"] == "PO-7731: please confirm the delivery date"
    assert "2000 m" in msg.get_content() and msg["Message-ID"].endswith("@workforce>")
    with db.session() as s:
        rec = s.execute(select(ActionRecord)).scalar_one()
        assert rec.status == "VERIFIED" and rec.external_ref == msg["Message-ID"]
    assert _item(wid).outcome == "ACTION_TAKEN"
