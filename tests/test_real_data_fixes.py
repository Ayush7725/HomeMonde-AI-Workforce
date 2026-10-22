"""Regressions found by running HomeMonde's real exports (Amazon order report, outsourced stock) through the platform."""
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml
from sqlalchemy import select

from conftest import ROOT, stockout_scenario, write_csv


def _sync_and_rules(env, cfg=None):
    from workforce import data, db, rules
    cfg = cfg or env.cfg
    with db.session() as s:
        res = data.sync_source(s, cfg, "exports")
    with db.session() as s:
        created = rules.evaluate(s, cfg)
    return res, created


def _issues():
    from workforce import db
    from workforce.models import WorkItem
    with db.session() as s:
        return {i.dedupe_key: (i.status, i.title, [st["text"] for st in i.finding["statements"]])
                for i in s.execute(select(WorkItem).where(WorkItem.type == "DATA_ISSUE")).scalars()}


def _add_order(env, sku, qty_per_day):
    """Recent sales of a SKU that the stock export does not list."""
    import csv
    from workforce.data import today
    path = env.exports / "order_lines.csv"
    rows = list(csv.DictReader(path.open()))
    t = today(env.cfg)
    rows += [{**rows[0], "order_id": f"X{d}", "order_date": (t - timedelta(days=d)).isoformat(), "sku": sku, "quantity": qty_per_day, "net_amount": 0}
             for d in range(7)]
    write_csv(path, rows)


def test_a_sku_missing_from_the_stock_export_is_unknown_not_zero(env):
    """Real data: 1,228 selling SKUs had no stock row and all showed 0 days of cover, raising false stockout alerts."""
    stockout_scenario(env.exports, env.cfg)
    _add_order(env, "HM-999", 30)
    _, created = _sync_and_rules(env)
    from workforce import db
    from workforce.metrics import MetricEngine
    from workforce.models import WorkItem
    with db.session() as s:
        e = MetricEngine(s, env.cfg)
        assert e.for_group("sellable_units", "sku", "HM-999") is None
        assert e.for_group("stock_coverage_days", "sku", "HM-999") is None
        groups = [i.context.get("group") for i in s.execute(select(WorkItem).where(WorkItem.type == "STOCKOUT_RISK")).scalars()]
    assert created == 1 and groups == ["HM-204"]
    status, title, facts = _issues()["data:exports:inventory:gaps:stockout_risk"]
    assert status == "NEEDS_HUMAN_REVIEW" and title == "Records missing · Inventory (WMS)"
    assert "1 sku values" in facts[0] and "HM-999" in facts[1] and "never as zero" in facts[2]
    # once the export lists it, the task closes by itself
    import csv
    inv = list(csv.DictReader((env.exports / "inventory.csv").open()))
    write_csv(env.exports / "inventory.csv", inv + [{**inv[0], "sku": "HM-999", "available": 9000, "reserved": 0}])
    _sync_and_rules(env)
    assert _issues()["data:exports:inventory:gaps:stockout_risk"][0] == "COMPLETED"


def test_a_blank_optional_field_does_not_drop_the_row(env):
    """Real data: a blank 'reserved' made 'available - reserved' unknown, so SKU stock read as 0 units."""
    stockout_scenario(env.exports, env.cfg)
    import csv
    inv = list(csv.DictReader((env.exports / "inventory.csv").open()))
    write_csv(env.exports / "inventory.csv", [{**r, "reserved": ""} for r in inv])
    _sync_and_rules(env)
    from workforce import data, db
    from workforce.metrics import MetricEngine
    from workforce.tenancy import FieldSpec
    with db.session() as s:
        assert MetricEngine(s, env.cfg).for_group("sellable_units", "sku", "HM-204") == 320  # the pack says blank = 0
    # without a default, a group whose every row is blank is unknown — never silently absent (which reads as zero)
    inv_ds = env.cfg.datasets["inventory"]
    no_default = inv_ds.model_copy(update={"fields": {**inv_ds.fields, "reserved": FieldSpec(type="integer")}})
    cfg = env.cfg.model_copy(update={"datasets": {**env.cfg.datasets, "inventory": no_default}})
    with db.session() as s:
        data.sync_source(s, cfg, "exports")
    with db.session() as s:
        e = MetricEngine(s, cfg)
        assert ("HM-204",) in e.values("sellable_units") and e.values("sellable_units")[("HM-204",)] is None


@pytest.mark.parametrize("raw, fmt, want", [
    ("2025-09-30T18:45:00+00:00", None, "2025-10-01"),   # after 18:30 UTC it is already the next day in India
    ("2025-09-30T18:29:00+00:00", None, "2025-09-30"),
    ("2025-09-30T23:10:00Z", None, "2025-10-01"),
    ("2025-09-30T18:45:00", None, "2025-09-30"),         # no offset: taken as already local
    ("2025-09-30", None, "2025-09-30"),
    ("30/09/2025 18:45 +0000", "%d/%m/%Y %H:%M %z", "2025-10-01"),
])
def test_timestamps_count_on_the_company_calendar_day(raw, fmt, want):
    """Real data: 34,498 Amazon order lines (8.4%) were dated a day early because the UTC date was cut from the text."""
    from workforce.data import _convert
    from workforce.tenancy import FieldSpec
    assert _convert(FieldSpec(type="date", format=fmt), raw, ZoneInfo("Asia/Kolkata")) == want


def test_one_missing_file_only_makes_its_own_dataset_stale(env):
    """Real data: with production_batches.csv absent, 410,592 good order lines were marked stale and later files never read."""
    stockout_scenario(env.exports, env.cfg)
    (env.exports / "production_batches.csv").unlink()
    res, created = _sync_and_rules(env)
    from workforce import data, db
    from workforce.models import Event, SourceState
    with db.session() as s:
        st = s.get(SourceState, ("homemonde", "exports"))
        assert st.status == "OK" and st.failed_datasets == ["production_batches"]
        assert res["inventory"] == 2 and res["purchase_orders"] == 1 and res["supplier_scorecard"] == 2
        assert {d: data.freshness(s, env.cfg, d)[0] for d in res} == {
            "inventory": "CURRENT", "order_lines": "CURRENT", "production_batches": "STALE",
            "purchase_orders": "CURRENT", "supplier_scorecard": "CURRENT"}
        sync = s.execute(select(Event).where(Event.type == "DATA_SYNC")).scalar_one()
        assert "1 of 5 files could not be used (Production plan)" in sync.msg
    assert created == 1  # the stockout rule reads stock and orders only, so it still works
    assert _issues()["data:exports:production_batches"][0] == "NEEDS_HUMAN_REVIEW"
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    with db.session() as s:
        assert data.freshness(s, env.cfg, "production_batches")[0] == "CURRENT"
    assert _issues()["data:exports:production_batches"][0] == "COMPLETED"


def test_a_refused_file_is_shown_as_out_of_date(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    write_csv(env.exports / "inventory.csv", [{"sku": "HM-204", "location": "JAIPUR-WH", "product_name": "x", "available": "lots",
                                                "reserved": 0, "incoming": 0}])
    from workforce import data, db
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
        assert data.freshness(s, env.cfg, "inventory")[0] == "STALE"
        assert data.freshness(s, env.cfg, "order_lines")[0] == "CURRENT"


def test_old_data_in_a_freshly_read_file_is_stale(env):
    """Real data: on 21 Oct, orders ending 31 Aug were shown as CURRENT and 'sales this week' as ₹0."""
    stockout_scenario(env.exports, env.cfg)
    import csv
    from workforce.data import today
    rows = list(csv.DictReader((env.exports / "order_lines.csv").open()))
    old = (today(env.cfg) - timedelta(days=10)).isoformat()
    write_csv(env.exports / "order_lines.csv", [{**r, "order_date": old} for r in rows])
    _, created = _sync_and_rules(env)
    from workforce import data, db
    with db.session() as s:
        state, label = data.freshness(s, env.cfg, "order_lines")
        assert state == "STALE" and "newest record" in label
        assert data.freshness(s, env.cfg, "inventory")[0] == "CURRENT"
    assert created == 0  # stockout reads order lines, so it does not fire on them


def test_a_missing_column_is_named_instead_of_failing_every_row(env):
    stockout_scenario(env.exports, env.cfg)
    import csv
    inv = list(csv.DictReader((env.exports / "inventory.csv").open()))
    write_csv(env.exports / "inventory.csv", [{("Item sku" if k == "sku" else k): v for k, v in r.items()} for r in inv])
    from workforce import data, db
    from workforce.models import Event
    with db.session() as s:
        assert data.sync_source(s, env.cfg, "exports")["inventory"] == 0
        ev = s.execute(select(Event).where(Event.type == "DATA_REJECTED")).scalar_one()
        assert "the file has no column 'sku'" in ev.msg and ev.payload["missing_columns"] == ["sku"]
    facts = _issues()["data:exports:inventory"][2]
    assert "no column named 'sku'" in facts[0] and "'Item sku'" in facts[1]


def test_check_names_a_tool_field_the_dataset_lacks(tmp_path):
    from workforce import tenancy
    pack = yaml.safe_load((ROOT / "tenants" / "homemonde" / "tenant.yaml").read_text())
    inv = pack["datasets"]["inventory"]
    inv["key"] = ["sku"]
    del inv["fields"]["location"]
    (tmp_path / "homemonde").mkdir()
    (tmp_path / "homemonde" / "tenant.yaml").write_text(yaml.safe_dump(pack))
    with pytest.raises(ValueError, match=r"tool get_inventory_position: fields \['location'\] not in inventory"):
        tenancy.load_file(tmp_path / "homemonde" / "tenant.yaml")


def _inventory(env):
    import csv
    return list(csv.DictReader((env.exports / "inventory.csv").open()))


def test_rows_that_contradict_each_other_are_left_out_not_picked_at_random(env):
    """Real data: two stock rows for one SKU+location overwrote each other (last one won) and the sync still counted both."""
    stockout_scenario(env.exports, env.cfg)
    # enough clean rows that 2 bad ones stay under the 5% reject limit
    inv = _inventory(env) + [{**_inventory(env)[1], "sku": f"HM-9{i:02}"} for i in range(40)]
    write_csv(env.exports / "inventory.csv", inv + [{**inv[0], "available": 9999}])
    from workforce import data, db
    from workforce.metrics import MetricEngine
    from workforce.models import Record
    with db.session() as s:
        assert data.sync_source(s, env.cfg, "exports")["inventory"] == 41  # everything except HM-204
    with db.session() as s:
        stored = {r.data["sku"] for r in s.execute(select(Record).where(Record.dataset == "inventory")).scalars()}
        assert "HM-204" not in stored and len(stored) == 41
        assert MetricEngine(s, env.cfg).for_group("sellable_units", "sku", "HM-204") is None
    status, title, facts = _issues()["data:exports:inventory:duplicate_keys"]
    assert status == "NEEDS_HUMAN_REVIEW" and title.startswith("Rows that contradict each other")
    assert "1 " in facts[0] and "All 2 of those rows were left out" in facts[0]
    assert "HM-204" in facts[1] and "rows 2, 44" in facts[1]
    # a clean file closes the task by itself
    write_csv(env.exports / "inventory.csv", inv)
    with db.session() as s:
        assert data.sync_source(s, env.cfg, "exports")["inventory"] == 42
    assert _issues()["data:exports:inventory:duplicate_keys"][0] == "COMPLETED"


def test_exact_copies_of_a_row_are_kept_once(env):
    stockout_scenario(env.exports, env.cfg)
    inv = _inventory(env)
    write_csv(env.exports / "inventory.csv", inv + [dict(inv[0]), dict(inv[0])])
    from workforce import data, db
    with db.session() as s:
        assert data.sync_source(s, env.cfg, "exports")["inventory"] == 2
    assert "data:exports:inventory:duplicate_keys" not in _issues()


def test_contradicting_rows_count_towards_the_reject_limit(env):
    """If most of a file contradicts itself the file is refused whole, like any other badly broken file."""
    stockout_scenario(env.exports, env.cfg)
    inv = _inventory(env)
    write_csv(env.exports / "inventory.csv", inv + [{**r, "available": 1} for r in inv])
    from workforce import data, db
    from workforce.models import Event
    with db.session() as s:
        assert data.sync_source(s, env.cfg, "exports")["inventory"] == 0
        ev = s.execute(select(Event).where(Event.type == "DATA_REJECTED")).scalar_one()
        assert "4 of 4 rows invalid" in ev.msg


def test_a_refused_file_shows_its_most_common_problems_first(env):
    """Real data: 410,592 rows failed on one column, but the examples shown were the first 3 rows with a different problem."""
    stockout_scenario(env.exports, env.cfg)
    import csv
    path = env.exports / "order_lines.csv"
    rows = list(csv.DictReader(path.open()))
    bad = [{**r, "sku": ""} for r in rows[:2]] + [{**r, "quantity": f"x{i}"} for i, r in enumerate(rows[2:])]
    write_csv(path, bad)
    from workforce import data, db
    from workforce.models import Event
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
        ev = next(e for e in s.execute(select(Event).where(Event.type == "DATA_REJECTED")).scalars()
                  if e.payload.get("dataset") == "order_lines")
        first, second = ev.payload["examples"][:2]
    n = len(rows) - 2
    assert first == f"row 4: quantity: invalid integer 'x0' ({n:,} rows like this)"
    assert second == "row 2: sku: required (2 rows like this)"
