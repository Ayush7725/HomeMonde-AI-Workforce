"""Regressions found by a 766,000-line generated stress dataset with planted dirty data, near-misses and a next day."""
import csv
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from conftest import stockout_scenario, write_csv


def _sync_and_rules(env, source="exports"):
    from workforce import data, db, rules
    with db.session() as s:
        res = data.sync_source(s, env.cfg, source)
    with db.session() as s:
        created = rules.evaluate(s, env.cfg)
    return res, created


def _items(type_):
    from workforce import db
    from workforce.models import WorkItem
    with db.session() as s:
        return [(i.status, i.context, i.outcome) for i in s.execute(select(WorkItem).where(WorkItem.type == type_)).scalars()]


def _issues():
    from workforce import db
    from workforce.models import WorkItem
    with db.session() as s:
        return {i.dedupe_key: (i.status, i.title, [st["text"] for st in i.finding["statements"]])
                for i in s.execute(select(WorkItem).where(WorkItem.type == "DATA_ISSUE")).scalars()}


def _rows(env, name):
    return list(csv.DictReader((env.exports / name).open()))


@pytest.mark.parametrize("raw,ok", [("1,299.00", 1299.0), ("₹ 4,500", 4500.0), ("(12)", -12.0), ("-3.5", -3.5), (".5", 0.5),
                                    ("1e3", None), ("1E+03", None), ("nan", None), ("inf", None), ("12 pcs", None), ("-", None)])
def test_only_plain_numbers_are_read(raw, ok):
    """Stress data: quantities written '1e3' read as 1,000 units and raised 35 false stockout alerts."""
    from workforce.data import _number
    if ok is None:
        with pytest.raises(ValueError):
            _number(raw)
    else:
        assert _number(raw) == ok


@pytest.mark.parametrize("raw,order,want", [
    ("09-Sep-2026", None, "2026-09-09"), ("9 September 2026", None, "2026-09-09"), ("Sep 9, 2026", None, "2026-09-09"),
    ("21/10/2026", None, "2026-10-21"), ("10/21/2026", None, "2026-10-21"), ("05/10/2026", "DMY", "2026-10-05"),
    ("05/10/2026", "MDY", "2026-05-10"), ("05.10.2026 14:05", "DMY", "2026-10-05"), ("05/10/2026", None, None), ("31/31/2026", None, None)])
def test_dates_with_a_month_name_or_a_known_day_order_are_read(raw, order, want):
    """Stress data: goods receipts dated '09-Sep-2026' were refused, so their invoices looked unreceived (7 false alerts)."""
    from workforce.data import _convert
    from workforce.tenancy import FieldSpec
    if want is None:
        with pytest.raises(ValueError):
            _convert(FieldSpec(type="date"), raw, None, order)
    else:
        assert _convert(FieldSpec(type="date"), raw, None, order) == want


def test_a_status_in_another_case_still_counts(env):
    """Stress data: 709 sales lines marked 'Delivered' or ' SHIPPED ' were left out of sales, and 'Amazon' became its
    own channel next to 'amazon'."""
    stockout_scenario(env.exports, env.cfg)
    rows = _rows(env, "order_lines.csv")
    rows = [{**r, "status": "Delivered", "channel": "Amazon"} if i % 5 == 0 and r["channel"] == "amazon" else r
            for i, r in enumerate(rows)]
    write_csv(env.exports / "order_lines.csv", rows)
    _sync_and_rules(env)
    from workforce import db
    from workforce.metrics import MetricEngine
    with db.session() as s:
        e = MetricEngine(s, env.cfg)
        channels = {g[0] for g in e.values("revenue_7d_by_channel")}
        assert channels == {"amazon", "d2c", "flipkart"}
        assert e.for_group("demand_7d", "sku", "HM-204") == 95


def test_an_event_dated_after_today_is_refused(env):
    """Stress data: order lines typed as 2027 counted in this week's sales."""
    stockout_scenario(env.exports, env.cfg)
    from workforce.data import today
    rows = _rows(env, "order_lines.csv")
    rows.append({**rows[0], "order_id": "TYPO", "order_date": (today(env.cfg) + timedelta(days=365)).isoformat(), "quantity": 5000})
    write_csv(env.exports / "order_lines.csv", rows)
    res, _ = _sync_and_rules(env)
    from workforce import db
    from workforce.metrics import MetricEngine
    from workforce.models import Event
    with db.session() as s:
        assert MetricEngine(s, env.cfg).for_group("demand_7d", "sku", "HM-204") == 95
        ev = s.execute(select(Event).where(Event.type == "DATA_REJECTED", Event.msg.contains("Order lines"))).scalar_one()
        assert "order_date: date after today" in ev.payload["examples"][0]


def test_a_ratio_exactly_at_the_limit_does_not_fire(env):
    """Stress data: ROAS of exactly 0.70x its 28-day level fired 'falling', because both sides were rounded first."""
    from workforce import data, db, rules
    from workforce.data import today
    t = today(env.cfg)
    # 7 days: 6,784,631.49 / 2,059,000. 28 days: 38,839,932.30 / 8,251,000. Exactly 0.70; rounded, 3.2951 < 0.7 x 4.7073.
    rows = [{"spend_date": (t - timedelta(days=d)).isoformat(), "channel": "amazon_ads", "campaign": "c",
             "spend": 2059000 if d == 0 else (8251000 - 2059000 if d == 7 else 0),
             "attributed_revenue": 6784631.49 if d == 0 else (38839932.30 - 6784631.49 if d == 7 else 0)} for d in range(28)]
    write_csv(env.exports / "marketing_spend.csv", rows)
    with db.session() as s:
        data.sync_source(s, env.cfg, "marketing")
    with db.session() as s:
        rules.evaluate(s, env.cfg)
        from workforce.metrics import MetricEngine
        assert MetricEngine(s, env.cfg).for_group("roas_7d", "channel", "amazon_ads") == 3.2951  # people still read 4 decimals
    assert _items("ROAS_DROP") == []


def test_a_header_only_file_is_refused_and_owned(env):
    write_csv(env.exports / "shipments.csv", [{"shipment_id": "S1", "carrier": "x", "ship_date": "2026-01-01", "status": "delivered"}])
    (env.exports / "shipments.csv").write_text("shipment_id,order_id,channel,carrier,ship_date,promised_date,delivered_date,status\n")
    res, _ = _sync_and_rules(env, "shipping")
    assert res["shipments"] == 0
    status, _, facts = _issues()["data:shipping:shipments"]
    assert status == "NEEDS_HUMAN_REVIEW" and "has no rows" in facts[0]


def test_a_file_that_stopped_moving_is_owned_and_said_so(env):
    """Stress data: production output ending 9 days ago synced as 'CURRENT' and nobody was told."""
    from workforce import db
    from workforce.data import today
    from workforce.models import Event
    old = today(env.cfg) - timedelta(days=9)
    write_csv(env.exports / "production_output.csv", [{"log_date": (old - timedelta(days=d)).isoformat(), "line": "CUT-1",
                                                      "planned_units": 100, "produced_units": 50} for d in range(7)])
    _sync_and_rules(env, "factory")
    status, title, facts = _issues()["data:factory:production_output:stale"]
    assert status == "NEEDS_HUMAN_REVIEW" and title.startswith("Data has stopped moving") and old.strftime("%d %b %Y") in facts[0]
    with db.session() as s:
        sync = s.execute(select(Event).where(Event.type == "DATA_SYNC")).scalars().all()[-1]
        assert "CURRENT" not in sync.msg and "out of date" in sync.msg
    assert _items("PRODUCTION_SHORTFALL") == []  # never alert on data that has stopped
    write_csv(env.exports / "production_output.csv", [{"log_date": today(env.cfg).isoformat(), "line": "CUT-1",
                                                      "planned_units": 100, "produced_units": 100}])
    _sync_and_rules(env, "factory")
    assert _issues()["data:factory:production_output:stale"][0] == "COMPLETED"


def test_blank_amounts_are_counted_and_owned(env):
    """Real and stress data: unpriced order lines were left out of sales totals without a word."""
    stockout_scenario(env.exports, env.cfg)
    rows = _rows(env, "order_lines.csv")
    write_csv(env.exports / "order_lines.csv", [{**r, "net_amount": ""} if i < 3 else r for i, r in enumerate(rows)])
    _sync_and_rules(env)
    status, title, facts = _issues()["data:exports:order_lines:blank_values"]
    assert status == "NEEDS_HUMAN_REVIEW" and f"3 of {len(rows)} rows have no net_amount" in facts[0]


def test_an_alert_closes_by_itself_when_the_stock_arrives(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    assert [st for st, _, _ in _items("STOCKOUT_RISK")] == ["QUEUED"]
    inv = _rows(env, "inventory.csv")
    write_csv(env.exports / "inventory.csv", [{**r, "available": 9000} if r["sku"] == "HM-204" else r for r in inv])
    _sync_and_rules(env)
    [(status, ctx, outcome)] = _items("STOCKOUT_RISK")
    assert status == "COMPLETED" and outcome == "CONDITION_CLEARED" and ctx["cleared"] == ["HM-204"]


def test_an_alert_stays_open_while_the_figure_is_unknown(env):
    """Unknown never clears: a SKU that drops out of the stock export is not 'fixed'."""
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    write_csv(env.exports / "inventory.csv", [r for r in _rows(env, "inventory.csv") if r["sku"] != "HM-204"])
    _sync_and_rules(env)
    assert [st for st, _, _ in _items("STOCKOUT_RISK")] == ["QUEUED"]


def test_a_batched_alert_records_what_cleared_and_closes_when_all_have(env):
    from workforce.data import today
    t = today(env.cfg)
    pos = [{"po_number": f"PO-{i}", "material_code": "FAB-1", "supplier": "S", "quantity": 10, "unit": "m",
            "due_date": (t - timedelta(days=3)).isoformat(), "status": "OPEN"} for i in range(3)]
    stockout_scenario(env.exports, env.cfg)
    write_csv(env.exports / "purchase_orders.csv", pos)
    _sync_and_rules(env)
    [(status, ctx, _)] = _items("PO_OVERDUE")
    assert ctx["groups"] == ["PO-0", "PO-1", "PO-2"] and status == "QUEUED"
    pos[0]["status"] = "RECEIVED"  # no longer an open order, so no longer overdue
    write_csv(env.exports / "purchase_orders.csv", pos)
    _sync_and_rules(env)
    [(status, ctx, _)] = _items("PO_OVERDUE")
    assert status == "QUEUED" and ctx["cleared"] == ["PO-0"]
    write_csv(env.exports / "purchase_orders.csv", [{**p, "status": "RECEIVED"} for p in pos])
    _sync_and_rules(env)
    assert [st for st, _, _ in _items("PO_OVERDUE")] == ["COMPLETED"]


def test_one_batch_can_make_several_products(env):
    """Stress data: a 14-product batch shared one batch_id, so every row was left out as contradicting the others."""
    stockout_scenario(env.exports, env.cfg)
    from workforce.data import today
    rows = [{**r, "planned_start": (today(env.cfg) - timedelta(days=2)).isoformat()} for r in _rows(env, "production_batches.csv")]
    write_csv(env.exports / "production_batches.csv", rows + [{**rows[0], "sku": "HM-118"}])
    _sync_and_rules(env)
    assert "data:exports:production_batches:duplicate_keys" not in _issues()
    [(status, ctx, _)] = _items("BATCH_BLOCKED")
    assert ctx["group"] == "PB-118" and ctx["evidence_pack"]["blocked_batch_lines"]["value"] == 2


def test_a_blocked_batch_not_yet_due_to_start_is_not_an_alert(env):
    stockout_scenario(env.exports, env.cfg)  # PB-118 is blocked but planned for tomorrow
    _sync_and_rules(env)
    assert _items("BATCH_BLOCKED") == []


def test_a_channel_that_stops_sending_orders_is_raised(env):
    """Stress data and real data: one channel stopped for 5 days while the export as a whole kept moving."""
    from workforce.data import today
    t = today(env.cfg)
    lines = []
    for d in range(30):
        for ch in ("amazon", "d2c"):
            if ch == "amazon" and d < 5:
                continue
            for k in range(3):
                lines.append({"order_id": f"{ch}{d}-{k}", "line_no": 1, "order_date": (t - timedelta(days=d)).isoformat(), "channel": ch,
                              "sku": "HM-118", "quantity": 1, "net_amount": 499, "status": "delivered", "customer_email": ""})
    stockout_scenario(env.exports, env.cfg)
    write_csv(env.exports / "order_lines.csv", lines)
    _sync_and_rules(env)
    [(status, ctx, _)] = _items("CHANNEL_SILENT")
    assert ctx["group"] == "amazon" and ctx["evidence_pack"]["days_since_last_order"]["value"] == 5
