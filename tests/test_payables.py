"""Accounts payable: every open invoice is matched against its order and the goods received, duplicates are caught
before the payment run, and the AI only explains and drafts the query; people hold or release payments."""
from datetime import timedelta

from sqlalchemy import select

from conftest import stockout_scenario, text, use_fake, write_csv
from test_workforce import _item, _login, _run_next


def _ap(env, invoices, receipts):
    """Orders PO-1 (100 at 50) and PO-2 (40 at 200), the given receipts and invoices, synced and checked."""
    from workforce import data, db, rules
    t = data.today(env.cfg)
    stockout_scenario(env.exports, env.cfg)
    write_csv(env.exports / "purchase_orders.csv", [
        {"po_number": po, "material_code": m, "supplier": s, "quantity": q, "unit": "m", "due_date": (t + timedelta(days=5)).isoformat(),
         "revised_eta": "", "status": "OPEN", "unit_price": p}
        for po, m, s, q, p in [("PO-1", "FAB-A", "Acme Mills", 100, 50), ("PO-2", "FAB-B", "Bharat Weaves", 40, 200)]])
    write_csv(env.exports / "goods_receipts.csv", [
        {"grn_number": g, "po_number": po, "material_code": "", "quantity": q, "received_date": (t - timedelta(days=3)).isoformat()} for g, po, q in receipts]
        or [{"grn_number": "GRN-0", "po_number": "PO-9", "material_code": "", "quantity": 1, "received_date": t.isoformat()}])
    write_csv(env.exports / "supplier_invoices.csv", [
        {"voucher_id": v, "invoice_number": n, "supplier": s, "po_number": po, "material_code": "", "quantity": q, "unit_price": p,
         "total_amount": q * p, "invoice_date": (t - timedelta(days=ago)).isoformat(), "status": st} for v, n, s, po, q, p, ago, st in invoices])
    for sid in ("exports", "payables"):
        with db.session() as s:
            data.sync_source(s, env.cfg, sid)
    with db.session() as s:
        rules.evaluate(s, env.cfg)


def _checks(kind):
    from workforce import db
    from workforce.models import WorkItem
    with db.session() as s:
        return {i.context["group"]: i.context["check"] for i in s.execute(select(WorkItem).where(WorkItem.type == kind)).scalars()}


def test_three_way_match_flags_only_real_mismatches(env):
    _ap(env, invoices=[("V1", "A-100", "Acme Mills", "PO-1", 100, 50, 5, "OPEN"),    # 90 received: billed 10 too many
                       ("V2", "B-200", "Bharat Weaves", "PO-2", 40, 210, 4, "OPEN"),  # 5% above the order price
                       ("V3", "A-101", "Acme Mills", "PO-1", 90, 50, 3, "PAID"),      # paid: not this check's business
                       ("V4", "C-300", "Acme Mills", "PO-9", 5, 50, 2, "OPEN")],      # no such order, nothing received
        receipts=[("G1", "PO-1", 90), ("G2", "PO-2", 40)])
    found = _checks("INVOICE_MISMATCH")
    assert set(found) == {"A-100", "B-200", "C-300"}
    a = found["A-100"]
    assert a["issues"] == ["billed more than received"] and a["excess_quantity"] == 10 and a["amount_at_risk"] == 500
    assert found["B-200"]["issues"] == ["price differs from the order"] and found["B-200"]["amount_at_risk"] == 400
    assert set(found["C-300"]["issues"]) == {"no purchase order", "no goods received"}


def test_duplicates_are_caught_before_payment_and_paid_pairs_are_left_alone(env):
    _ap(env, invoices=[("V1", "INV-0457", "Acme Mills", "PO-1", 100, 50, 10, "OPEN"),
                       ("V2", "inv 457", "Acme Mills", "PO-1", 100, 50, 6, "OPEN"),      # same number, written differently
                       ("V3", "B-900", "Bharat Weaves", "PO-2", 40, 200, 30, "OPEN"),
                       ("V4", "B-917", "Bharat Weaves", "PO-2", 40, 200, 2, "OPEN"),     # same amount again within 45 days
                       ("V5", "A-1", "Acme Mills", "PO-1", 7, 50, 200, "PAID"),
                       ("V6", "A-1", "Acme Mills", "PO-1", 7, 50, 199, "PAID")],         # both paid: not a payment to stop
        receipts=[("G1", "PO-1", 100), ("G2", "PO-2", 40)])
    found = _checks("DUPLICATE_INVOICE")
    assert set(found) == {"V2", "V4"}
    assert found["V2"]["reason"] == "same invoice number" and found["V4"]["reason"] == "same amount within the window"
    from workforce import db, rules
    with db.session() as s:  # each suspect is raised once, not on every pass
        assert rules.evaluate(s, env.cfg) == 0


def test_without_ai_the_mismatch_is_reported_in_plain_verified_words(env):
    _ap(env, invoices=[("V1", "A-100", "Acme Mills", "PO-1", 100, 50, 5, "OPEN")], receipts=[("G1", "PO-1", 90)])
    from workforce import db, work
    from workforce.models import WorkItem
    with db.session() as s:
        stock = s.execute(select(WorkItem).where(WorkItem.rule_id == "stockout_risk")).scalar_one()
        work.transition(s, env.cfg, stock, "COMPLETED", "not part of this test", outcome="STOPPED")
    wid = _run_next(env)
    texts = [s["text"] for s in _item(wid).finding["statements"]]
    assert texts == ["Invoice A-100 from Acme Mills bills 100 at INR 50 each against order PO-1.", "The order is for 100 at INR 50 each.",
                     "90 have been received on PO-1.", "INR 500 would be overpaid if the invoice is paid as billed."]


def test_the_ai_drafts_the_supplier_query_and_accounts_approve_it(env, client):
    from workforce import db, security, work
    from workforce.models import ActionRecord, User, WorkItem
    _ap(env, invoices=[("V1", "A-100", "Acme Mills", "PO-1", 100, 50, 5, "OPEN")], receipts=[("G1", "PO-1", 90)])
    with db.session() as s:
        stock = s.execute(select(WorkItem).where(WorkItem.rule_id == "stockout_risk")).scalar_one()
        work.transition(s, env.cfg, stock, "COMPLETED", "not part of this test", outcome="STOPPED")

    def final(kw):
        import json
        m1 = json.loads(kw["messages"][0]["content"])["evidence_already_gathered"][0]["ref"]
        return [text({"outcome": "FINDING", "headline": "Acme Mills billed 10 more units than were delivered on PO-1",
                      "statements": [{"kind": "FACT", "text": "Invoice A-100 bills 100 units but 90 were received.", "refs": [m1]}],
                      "evidence_quality": "CONFIRMED", "impact": "INR 500 would be overpaid.", "unknowns": [],
                      "recommendation": {"action": "raise_invoice_query", "summary": "Ask Acme Mills for a credit note for 10 units",
                                         "rationale": "Only 90 of 100 units were received.", "refs": [m1],
                                         "params": [{"name": "supplier", "value": "Acme Mills"}, {"name": "invoice_number", "value": "A-100"},
                                                    {"name": "issue", "value": "quantity"},
                                                    {"name": "message", "value": "Invoice A-100 bills 100 units but we received 90 on PO-1. "
                                                                                 "Please send a credit note for 10 units (INR 500)."}]}})]
    use_fake([final])
    wid = _run_next(env)
    assert _item(wid).status == "NEEDS_HUMAN_REVIEW"
    with db.session() as s:
        s.add(User(tenant_id="homemonde", email="accounts@homemonde.in", name="Accounts", role="manager", areas=["finance"],
                   password_hash=security.hash_password("correct horse battery")))
    csrf = _login(client, "accounts@homemonde.in")
    ap = client.get("/api/state").json()["approvals"][0]
    assert ap["action"] == "raise_invoice_query" and ap["params"]["issue"] == "quantity"
    assert client.post(f"/api/approvals/{ap['id']}/decision", headers={"X-CSRF-Token": csrf}, json={"decision": "APPROVED"}).status_code == 200
    with db.session() as s:
        assert s.execute(select(ActionRecord)).scalar_one().status == "VERIFIED"
