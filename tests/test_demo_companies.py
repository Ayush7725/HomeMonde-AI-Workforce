"""Four differently shaped companies on one platform: every planted situation is found, nothing else
fires, bad rows are skipped, broken sources fail visibly, and tenants never see each other's data."""
import shutil
import sys

import pytest
from sqlalchemy import select

from conftest import ROOT, database_url, fresh_schema, text, tool_use, use_fake

sys.path.insert(0, str(ROOT / "demo"))
from generate import EXPECTED, FAILING_SOURCES, generate  # noqa: E402

COMPANIES = list(EXPECTED)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    tenants = tmp_path / "tenants"
    for slug in COMPANIES:
        shutil.copytree(ROOT / ("tenants" if slug == "homemonde" else "demo/tenants") / slug, tenants / slug)
    for k, v in generate(tmp_path / "data").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WORKFORCE_DATABASE_URL", database_url(tmp_path, "demo.db"))
    monkeypatch.setenv("WORKFORCE_TENANTS_DIR", str(tenants))
    monkeypatch.setenv("WORKFORCE_TENANTS", ",".join(COMPANIES))
    monkeypatch.setenv("WORKFORCE_SECURE_COOKIES", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from workforce import data, db, rules, settings, tenancy
    from workforce.providers import AnthropicAdapter, anthropic_client
    settings.get_settings.cache_clear()
    tenancy.get.cache_clear()
    db.configure()
    fresh_schema()
    with db.session() as s:
        cfgs = tenancy.register_all(s)
    for cfg in cfgs.values():
        for sid in cfg.sources:
            with db.session() as s:
                data.sync_source(s, cfg, sid)
        with db.session() as s:
            rules.evaluate(s, cfg)
    yield cfgs
    AnthropicAdapter.client_factory = staticmethod(anthropic_client)
    db.engine().dispose()
    settings.get_settings.cache_clear()
    tenancy.get.cache_clear()


def _items(tenant):
    from workforce import db
    from workforce.models import WorkItem
    with db.session() as s:
        return [(w.type, str(w.context.get("group")), w.id) for w in
                s.execute(select(WorkItem).where(WorkItem.tenant_id == tenant)).scalars()]


@pytest.mark.parametrize("tenant", COMPANIES)
def test_rules_find_exactly_the_planted_situations(demo, tenant):
    found = {(t, g) for t, g, _ in _items(tenant)}
    assert found == EXPECTED[tenant]


@pytest.mark.parametrize("tenant", COMPANIES)
def test_sources_sync_bad_rows_are_skipped_and_broken_feeds_fail_visibly(demo, tenant):
    from workforce import db
    from workforce.models import Event, SourceState
    with db.session() as s:
        states = {st.source_id: st for st in s.execute(select(SourceState).where(SourceState.tenant_id == tenant)).scalars()}
        failing = FAILING_SOURCES.get(tenant, set())
        assert {k for k, st in states.items() if st.status == "FAILED"} == failing
        assert all(st.rows > 0 for k, st in states.items() if k not in failing)
        rejected = s.execute(select(Event).where(Event.tenant_id == tenant, Event.type == "DATA_REJECTED")).scalars().all()
        assert rejected and all("skipped" in e.msg for e in rejected)  # a few bad rows never sink a whole batch
        if failing:
            assert s.execute(select(Event).where(Event.tenant_id == tenant, Event.type == "DATA_SOURCE_FAILURE")).first()


def test_different_formats_normalise_to_the_same_shapes(demo):
    from workforce import data, db
    with db.session() as s:
        atlas = data.rows(s, demo["atlas"], "sales_orders")
        assert all(len(r["order_date"]) == 10 and r["order_date"][4] == "-" for r in atlas)  # MM/DD/YYYY -> ISO
        assert any(isinstance(r["amount"], float) and r["amount"] > 1000 for r in atlas)        # "$1,234.50" -> 1234.5
        assert {r["on_hand"] for r in data.rows(s, demo["atlas"], "wms_stock") if r["stock_key"] == "BRK-1042@CHI"} == {1200}
        scores = {r["vendor"]: r["on_time_pct"] for r in data.rows(s, demo["atlas"], "vendors")}
        assert scores["Midwest Friction Co"] == 64.0                                            # "64%" -> 64
        bloom = data.rows(s, demo["bloom"], "orders")
        assert bloom and all(r["order_date"][4] == "-" for r in bloom)                          # DD/MM/YYYY via ';' CSV
        kavya = data.rows(s, demo["kavya"], "stock_batches")
        assert {"sku", "batch_no", "warehouse", "quantity", "expiry_date"} <= set(kavya[0])      # ERP columns mapped
        assert data.freshness(s, demo["bloom"], "ad_spend")[0] == "NEVER_SYNCED"


def test_every_company_investigates_its_work_without_ai(demo):
    from workforce import db, runtime, work
    from workforce.models import WorkItem
    for tenant, cfg in demo.items():
        while True:
            with db.session() as s:
                item = work.claim(s, [tenant], 600)
                wid = item.id if item else None
            if not wid:
                break
            with db.session() as s:
                runtime.run(s, cfg, s.get(WorkItem, (tenant, wid)))
        with db.session() as s:
            items = s.execute(select(WorkItem).where(WorkItem.tenant_id == tenant)).scalars().all()
            assert items and all(i.finding["statements"] for i in items), tenant
        for i in items:  # urgent items whose sources disagree wait for a person; everything else closes
            weak_urgent = i.priority in ("CRITICAL", "HIGH") and i.finding["evidence_quality"] == "DATA_CONFLICT"
            assert i.status == ("NEEDS_HUMAN_REVIEW" if weak_urgent else "COMPLETED"), (tenant, i.id, i.status)
            conflict = [i for i in items if i.type == "DATA_CONFLICT"]
            assert all(i.finding["evidence_quality"] == "DATA_CONFLICT" for i in conflict)


def test_level_one_action_runs_without_approval(demo):
    """Atlas lets Inventory AI move stock between its own DCs automatically; it is still checked and audited."""
    from workforce import db, events, runtime, work
    from workforce.models import ActionRecord, Approval, WorkItem
    target = next(wid for t, g, wid in _items("atlas") if t == "STOCKOUT_RISK")

    def investigate(kw):
        return [tool_use("get_stock_by_dc", {"part": "ALT-2210"}, 1)]

    def conclude(kw):
        return [text({"outcome": "FINDING", "headline": "ALT-2210 is short in Chicago while Dallas holds stock",
                      "statements": [{"kind": "FACT", "text": "Chicago holds 42 units and Dallas holds 28 units.", "refs": ["E1"]}],
                      "evidence_quality": "LIKELY", "impact": "", "unknowns": [],
                      "recommendation": {"action": "transfer_stock", "summary": "Move 20 units of ALT-2210 from Dallas to Chicago",
                                         "rationale": "Chicago serves most orders.", "refs": ["E1"],
                                         "params": [{"name": "part", "value": "ALT-2210"}, {"name": "quantity", "value": "20"},
                                                    {"name": "from_dc", "value": "DAL"}, {"name": "to_dc", "value": "CHI"}]}})]

    use_fake([investigate, conclude])
    with db.session() as s:
        item = s.get(WorkItem, ("atlas", target))
        item.lease_until = None
        runtime.run(s, demo["atlas"], item)
    with db.session() as s:
        item = s.get(WorkItem, ("atlas", target))
        assert item.status == "COMPLETED" and item.outcome == "ACTION_TAKEN"
        assert s.execute(select(Approval).where(Approval.work_id == target)).first() is None
        rec = s.execute(select(ActionRecord).where(ActionRecord.work_id == target)).scalar_one()
        assert rec.status == "VERIFIED" and rec.params == {"part": "ALT-2210", "quantity": 20, "from_dc": "DAL", "to_dc": "CHI"}
        assert events.verify_chain(s, "atlas") == (True, None)
        assert work.TERMINAL >= {item.status}


def test_each_company_sees_only_its_own_world(demo):
    from fastapi.testclient import TestClient
    from workforce import db, security
    from workforce.api import create_app
    from workforce.models import User
    with db.session() as s:
        for tenant in ("kavya", "bloom"):
            s.add(User(tenant_id=tenant, email=f"owner@{tenant}.test", name=f"{tenant} owner", role="owner",
                       password_hash=security.hash_password("correct horse battery")))
    kavya_ids, bloom_ids = {w for *_, w in _items("kavya")}, {w for *_, w in _items("bloom")}
    shared = sorted(kavya_ids & bloom_ids)        # ids are numbered per company, so they can repeat
    kavya_only = sorted(kavya_ids - bloom_ids)[0]
    with TestClient(create_app()) as c:
        c.post("/api/auth/login", json={"email": "owner@bloom.test", "password": "correct horse battery"})
        boot = c.get("/api/bootstrap").json()
        assert boot["tenant"]["name"] == "Bloom Apparel" and "care_centre" in boot["layout"]["areas"]
        assert set(boot["employees"]) == {"executive_ai", "customer_ai", "inventory_ai", "marketing_ai"}
        assert c.get(f"/api/work/{kavya_only}").status_code == 404
        for wid in shared:  # the same id resolves to Bloom's own item, never Kavya's
            assert c.get(f"/api/work/{wid}").json()["item"]["title"].split(" · ")[1].startswith("BL-")
        assert all(e.get("work_id") != kavya_only for e in c.get("/api/events").json()["events"])
        stale = {s["label"]: s["freshness"] for s in c.get("/api/state").json()["sources"]}
        assert stale["Ads platform feed"] == "NEVER_SYNCED" and stale["Web store exports"] == "CURRENT"
