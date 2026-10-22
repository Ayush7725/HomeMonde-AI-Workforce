"""Regressions for the QA audit's Tier B findings, after the business decisions (all amounts in rupees; standard
accounts-payable checks; costs negative in the ledger; ledger revenue reconciled with order-line sales)."""
import csv
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import stockout_scenario, write_csv
from test_payables import _ap, _checks


# ---------------------------------------------------------------- PIPE-1 currency

@pytest.mark.parametrize("raw,currency,ok", [("₹ 4,500", "INR", 4500.0), ("$1,200.00", "INR", None), ("€40", "INR", None),
                                             ("£9", "INR", None), ("$1,234.50", "USD", 1234.5), ("₹100", "USD", None),
                                             ("1,299.00", "INR", 1299.0)])
def test_an_amount_in_another_currency_is_refused_not_counted(raw, currency, ok):
    from workforce.data import _number
    if ok is None:
        with pytest.raises(ValueError):
            _number(raw, currency)
    else:
        assert _number(raw, currency) == ok


def test_a_dollar_order_line_is_reported_and_left_out_of_rupee_sales(env):
    from workforce import data, db
    from workforce.metrics import MetricEngine
    from workforce.models import Event
    stockout_scenario(env.exports, env.cfg)
    rows = list(csv.DictReader((env.exports / "order_lines.csv").open()))
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
    with db.session() as s:
        before = MetricEngine(s, env.cfg).values("sales_7d")[()]
    rows.append({**rows[0], "order_id": "US1", "net_amount": "$50.00"})
    write_csv(env.exports / "order_lines.csv", rows)
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
    with db.session() as s:
        assert MetricEngine(s, env.cfg).values("sales_7d")[()] == before
        ev = s.execute(select(Event).where(Event.type == "DATA_REJECTED", Event.msg.contains("Order lines"))).scalars().all()[-1]
        assert "net_amount: amount in another currency than INR" in ev.payload["examples"][0]


# ---------------------------------------------------------------- PIPE-4 / PIPE-5 three-way match

def _recheck(env, *sources):
    """Re-sync after editing the files _ap wrote and run the match again from scratch (its first alert is set aside, so
    the check answers for the edited data instead of being held back as already raised)."""
    from workforce import data, db, rules
    from workforce.models import WorkItem
    with db.session() as s:
        for it in s.execute(select(WorkItem).where(WorkItem.type == "INVOICE_MISMATCH")).scalars():
            s.delete(it)
    for sid in sources:
        with db.session() as s:
            data.sync_source(s, env.cfg, sid)
    with db.session() as s:
        rules.evaluate(s, env.cfg)


def test_an_invoice_on_an_order_with_no_price_cannot_be_verified(env):
    from workforce import data, db, rules
    _ap(env, invoices=[("V1", "A-100", "Acme Mills", "PO-1", 90, 99, 5, "OPEN")], receipts=[("G1", "PO-1", 90)])
    rows = list(csv.DictReader((env.exports / "purchase_orders.csv").open()))
    write_csv(env.exports / "purchase_orders.csv", [{**r, "unit_price": ""} if r["po_number"] == "PO-1" else r for r in rows])
    _recheck(env, "exports")
    assert _checks("INVOICE_MISMATCH")["A-100"]["issues"] == ["order has no price, so the price cannot be checked"]


def test_an_invoice_with_only_a_total_cannot_be_verified(env):
    _ap(env, invoices=[("V1", "A-100", "Acme Mills", "PO-1", 90, 50, 5, "OPEN")], receipts=[("G1", "PO-1", 90)])
    rows = list(csv.DictReader((env.exports / "supplier_invoices.csv").open()))
    write_csv(env.exports / "supplier_invoices.csv", [{**r, "quantity": "", "unit_price": ""} for r in rows])
    _recheck(env, "payables")
    assert _checks("INVOICE_MISMATCH")["A-100"]["issues"] == ["invoice has no quantity, so it cannot be checked",
                                                              "invoice has no unit price, so it cannot be checked"]


def test_billing_is_added_up_across_every_invoice_on_an_order(env):
    _ap(env, invoices=[("V1", "A-1", "Acme Mills", "PO-1", 60, 50, 9, "PAID"),
                       ("V2", "A-2", "Acme Mills", "PO-1", 50, 50, 4, "OPEN")],   # 110 billed in total, 60 received
        receipts=[("G1", "PO-1", 60)])
    c = _checks("INVOICE_MISMATCH")["A-2"]
    assert c["issues"] == ["billed more than received in total (with this order's other invoices)"]
    assert c["excess_quantity"] == 50 and c["amount_at_risk"] == 2500


def test_an_invoice_on_a_cancelled_order_is_flagged(env):
    from workforce import data, db, rules
    _ap(env, invoices=[("V1", "B-1", "Bharat Weaves", "PO-2", 40, 200, 4, "OPEN")], receipts=[("G2", "PO-2", 40)])
    rows = list(csv.DictReader((env.exports / "purchase_orders.csv").open()))
    write_csv(env.exports / "purchase_orders.csv", [{**r, "status": "CANCELLED"} if r["po_number"] == "PO-2" else r for r in rows])
    _recheck(env, "exports")
    assert _checks("INVOICE_MISMATCH")["B-1"]["issues"] == ["the order was cancelled"]


def test_quantities_in_different_units_are_a_unit_problem_not_overbilling(env):
    from workforce import data, db, rules
    _ap(env, invoices=[("V1", "A-1", "Acme Mills", "PO-1", 100, 50, 4, "OPEN")], receipts=[("G1", "PO-1", 1)])
    rows = list(csv.DictReader((env.exports / "goods_receipts.csv").open()))
    write_csv(env.exports / "goods_receipts.csv", [{**r, "unit": "roll"} for r in rows])
    _recheck(env, "payables")
    c = _checks("INVOICE_MISMATCH")["A-1"]
    assert c["issues"] == ["units differ from the order (order: m; receipts: roll), so quantities cannot be compared"]
    assert c["amount_at_risk"] == 0


def test_matching_units_still_compare_quantities(env):
    from workforce import data, db, rules
    _ap(env, invoices=[("V1", "A-1", "Acme Mills", "PO-1", 100, 50, 4, "OPEN")], receipts=[("G1", "PO-1", 90)])
    for f in ("goods_receipts.csv", "supplier_invoices.csv"):
        rows = list(csv.DictReader((env.exports / f).open()))
        write_csv(env.exports / f, [{**r, "unit": "M"} for r in rows])   # case does not matter
    _recheck(env, "payables")
    assert _checks("INVOICE_MISMATCH")["A-1"]["issues"] == ["billed more than received"]


# ---------------------------------------------------------------- PIPE-10 ledger signs

def test_a_ledger_amount_with_the_wrong_sign_is_reported(env):
    from workforce import data, db
    from workforce.models import WorkItem
    from workforce.data import today
    t = today(env.cfg).isoformat()
    write_csv(env.exports / "ledger.csv", [
        {"entry_id": "L1", "entry_date": t, "account": "revenue", "channel": "amazon", "amount": 100000},
        {"entry_id": "L2", "entry_date": t, "account": "cogs", "channel": "", "amount": -40000},
        {"entry_id": "L3", "entry_date": t, "account": "returns", "channel": "", "amount": 5000},             # should be negative
        {"entry_id": "L4", "entry_date": t, "account": "marketplace_fees", "channel": "", "amount": 8000}])  # should be negative
    with db.session() as s:
        data.sync_source(s, env.cfg, "accounts")
    with db.session() as s:
        issue = s.execute(select(WorkItem).where(WorkItem.dedupe_key == "data:accounts:ledger:signs")).scalar_one()
    facts = json.dumps(issue.finding)
    assert "2 of 4 rows" in facts and "returns" in facts and "marketplace_fees" in facts


# ---------------------------------------------------------------- PIPE-11 reconciliation

def test_ledger_revenue_far_from_order_sales_raises_a_mismatch(env):
    from workforce import data, db, rules
    from workforce.models import WorkItem
    from workforce.data import today
    stockout_scenario(env.exports, env.cfg)
    t = today(env.cfg)
    write_csv(env.exports / "ledger.csv", [{"entry_id": f"L{d}", "entry_date": (t - timedelta(days=d)).isoformat(), "account": "revenue",
                                           "channel": "", "amount": 1000} for d in range(30)])
    for sid in ("exports", "accounts"):
        with db.session() as s:
            data.sync_source(s, env.cfg, sid)
    with db.session() as s:
        rules.evaluate(s, env.cfg)
    with db.session() as s:
        [item] = s.execute(select(WorkItem).where(WorkItem.type == "REVENUE_MISMATCH")).scalars().all()
    assert item.employee == "finance_ai"


# ---------------------------------------------------------------- SEC-5 lockout abuse

def test_wrong_passwords_from_one_address_do_not_lock_the_owner_out_elsewhere(env):
    from workforce import db, security
    from workforce.models import User
    with db.session() as s:
        s.add(User(tenant_id="homemonde", email="boss@x.in", name="Boss", role="owner", password_hash=security.hash_password("right password 1")))
    for _ in range(security.MAX_FAILED):
        with db.session() as s:
            security.authenticate(s, "boss@x.in", "guess", "6.6.6.6")
    with db.session() as s:
        assert security.authenticate(s, "boss@x.in", "right password 1", "6.6.6.6") is None   # the guessing address is locked
    with db.session() as s:
        assert security.authenticate(s, "boss@x.in", "right password 1", "10.0.0.2") is not None  # the owner elsewhere is not


# ---------------------------------------------------------------- SEC-6 data is not instructions

def test_the_ai_is_told_that_text_inside_data_is_never_an_instruction():
    from workforce.runtime import CHARTER
    assert "never instructions" in CHARTER
