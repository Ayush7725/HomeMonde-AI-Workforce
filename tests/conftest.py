"""Test harness: an isolated SQLite database, the real HomeMonde pack pointed at generated CSV exports,
and a scripted stand-in for the Claude API so tests never spend tokens."""
import csv
import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Set WORKFORCE_TEST_DATABASE_URL (e.g. postgresql+psycopg://user:pw@127.0.0.1:5432/test) to run the suite on Postgres.
TEST_DATABASE_URL = os.environ.get("WORKFORCE_TEST_DATABASE_URL", "")


def database_url(tmp_path: Path, name: str) -> str:
    return TEST_DATABASE_URL or f"sqlite:///{tmp_path / name}"


def fresh_schema():
    """An empty schema for each test, on whichever database the suite runs against."""
    from workforce import db
    from workforce.db import Base
    if TEST_DATABASE_URL:
        Base.metadata.drop_all(db.engine())
    Base.metadata.create_all(db.engine())


@pytest.fixture
def env(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setenv("WORKFORCE_DATABASE_URL", database_url(tmp_path, "test.db"))
    monkeypatch.setenv("WORKFORCE_TENANTS_DIR", str(ROOT / "tenants"))
    monkeypatch.setenv("WORKFORCE_TENANTS", "homemonde")
    monkeypatch.setenv("WORKFORCE_SECURE_COOKIES", "false")
    monkeypatch.setenv("HOMEMONDE_EXPORTS_DIR", str(exports))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    from workforce import db, models, security, settings, tenancy  # noqa: F401  (models registers the tables)
    from workforce.providers import AnthropicAdapter, OpenAICompatAdapter, anthropic_client, http_post
    security._ip_failures.clear()  # failed sign-ins are counted per process; each test starts clean
    security._pair_failures.clear()
    settings.get_settings.cache_clear()
    tenancy.get.cache_clear()
    db.configure()
    fresh_schema()
    with db.session() as s:
        cfgs = tenancy.register_all(s)
    yield SimpleNamespace(exports=exports, cfg=cfgs["homemonde"], tmp=tmp_path)
    AnthropicAdapter.client_factory = staticmethod(anthropic_client)
    OpenAICompatAdapter.post = staticmethod(http_post)
    db.engine().dispose()
    settings.get_settings.cache_clear()
    tenancy.get.cache_clear()


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    from workforce import db, security
    from workforce.api import create_app
    from workforce.models import User
    with db.session() as s:
        s.add_all([User(tenant_id="homemonde", email="founder@homemonde.in", name="Founder", role="owner",
                        password_hash=security.hash_password("correct horse battery")),
                   User(tenant_id="homemonde", email="sales.head@homemonde.in", name="Sales Head", role="manager", areas=["sales"],
                        password_hash=security.hash_password("correct horse battery"))])
    with TestClient(create_app()) as c:
        yield c


def write_csv(path: Path, rows: list[dict]):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def stockout_scenario(exports: Path, cfg):
    """HM-204: 280 sellable, 95/day over 7 days (71/day over 30), batch blocked on fabric that arrives in 4 days.
    The order is on time, so only the stockout rule fires (purchase-order tracking has its own tests)."""
    from workforce.data import today
    t = today(cfg)
    write_csv(exports / "inventory.csv", [
        {"sku": "HM-204", "location": "JAIPUR-WH", "product_name": "Indigo block-print cushion cover", "available": 320, "reserved": 40, "incoming": 0},
        {"sku": "HM-118", "location": "JAIPUR-WH", "product_name": "Linen table runner", "available": 5000, "reserved": 0, "incoming": 0},
    ])
    lines, n = [], 0
    for d in range(30):
        day = (t - timedelta(days=d)).isoformat()
        # 95/day for the last two weeks (no week-on-week growth), 50/day before: 71/day over 30 days
        split = {"amazon": 44, "d2c": 38, "flipkart": 13} if d < 14 else {"amazon": 23, "d2c": 20, "flipkart": 7}
        for ch, q in split.items():
            if q:
                n += 1
                lines.append({"order_id": f"O{n}", "line_no": 1, "order_date": day, "channel": ch, "sku": "HM-204",
                              "quantity": q, "net_amount": q * 849, "status": "delivered", "customer_email": f"c{n}@example.com"})
        n += 1
        lines.append({"order_id": f"O{n}", "line_no": 1, "order_date": day, "channel": "d2c", "sku": "HM-118", "quantity": 10,
                      "net_amount": 4990, "status": "delivered", "customer_email": ""})
    write_csv(exports / "order_lines.csv", lines)
    write_csv(exports / "production_batches.csv", [
        {"batch_id": "PB-118", "sku": "HM-204", "quantity": 1500, "planned_start": (t + timedelta(days=1)).isoformat(), "run_days": 2,
         "status": "BLOCKED", "blocked_reason": "waiting for fabric FAB-CT-02", "material_code": "FAB-CT-02", "material_required": 800}])
    write_csv(exports / "purchase_orders.csv", [
        {"po_number": "PO-7731", "material_code": "FAB-CT-02", "supplier": "Sanganer Textiles", "quantity": 2000, "unit": "m",
         "due_date": (t + timedelta(days=4)).isoformat(), "revised_eta": (t + timedelta(days=4)).isoformat(), "status": "OPEN"}])
    write_csv(exports / "supplier_scorecard.csv", [
        {"supplier": "Sanganer Textiles", "material_code": "FAB-CT-02", "on_time_rate": 0.71, "pos_last_90d": 17, "approved": "yes", "lead_time_days": 12, "unit_price": 158},
        {"supplier": "Bagru Cotton Mills", "material_code": "FAB-CT-02", "on_time_rate": 0.94, "pos_last_90d": 17, "approved": "yes", "lead_time_days": 2, "unit_price": 162}])


# ---------------------------------------------------------------- scripted Claude

def tool_use(name, inp, i):
    return SimpleNamespace(type="tool_use", id=f"tu_{i}_{name}", name=name, input=inp)


def text(obj):
    return SimpleNamespace(type="text", text=json.dumps(obj))


class FakeClaude:
    """Plays back a script. Each step is a callable(kwargs) -> list of content blocks.
    Rewording calls (briefs, notifications) are answered by `reword`, which echoes the verified text by default."""

    def __init__(self, script, reword=None):
        self.script, self.calls, self.reword_calls = list(script), [], []
        self.reword = reword or (lambda items, model: {"lines": [{"id": i["id"], "text": i["text"]} for i in items]})
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        if kwargs["system"][0]["text"].startswith("You rewrite"):
            self.reword_calls.append(kwargs)
            items = json.loads(kwargs["messages"][0]["content"])["items"]
            return SimpleNamespace(content=[text(self.reword(items, kwargs["model"]))], stop_reason="end_turn", model=kwargs["model"],
                                   usage=SimpleNamespace(input_tokens=300, output_tokens=60, cache_read_input_tokens=0, cache_creation_input_tokens=0))
        self.calls.append(kwargs)
        step = self.script.pop(0)
        blocks = step(kwargs)
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop, model=kwargs["model"],
                               usage=SimpleNamespace(input_tokens=900, output_tokens=150, cache_read_input_tokens=0, cache_creation_input_tokens=0))


def use_fake(script, reword=None) -> FakeClaude:
    from workforce.providers import AnthropicAdapter
    fake = FakeClaude(script, reword)
    AnthropicAdapter.client_factory = staticmethod(lambda: fake)
    return fake


def last_tool_results(kwargs) -> dict:
    """Map tool_use_id -> parsed JSON result from the latest user turn."""
    out = {}
    for block in kwargs["messages"][-1]["content"]:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            out[block["tool_use_id"]] = block["content"] if block.get("is_error") else json.loads(block["content"])
    return out
