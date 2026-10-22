"""Real-browser tests of the web app, for the ways real people broke it in live testing: a second tab that is out of
date, a session that ended, no internet, numbers outside the allowed range, typing while the page updates, links to
things that do not exist. Starts the real server on a free port and drives headless Chrome (tests/e2e/browser.mjs).
Skipped when Chrome or Node is not installed."""
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from conftest import stockout_scenario

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "e2e" / "browser.mjs"
CHROME = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
NODE = shutil.which("node")
PASSWORD = "browser test password"
MODEL = "gemini:gemini-3.1-flash-lite"

pytestmark = pytest.mark.skipif(not (CHROME and NODE), reason="needs Google Chrome and Node.js")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(env):
    import uvicorn
    from workforce.api import create_app
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=10)


def _setup(env) -> dict:
    """Two people, one task and four pending suggestions, as the AI employees would leave them."""
    from workforce import actions, data, db, rules, runtime, security
    from workforce.models import Approval, User, WorkItem
    stockout_scenario(env.exports, env.cfg)
    with db.session() as s:
        data.sync_source(s, env.cfg, "exports")
    with db.session() as s:
        rules.evaluate(s, env.cfg)
    with db.session() as s:
        s.add_all([User(tenant_id="homemonde", email="owner@e2e.test", name="Test Owner", role="owner",
                        password_hash=security.hash_password(PASSWORD)),
                   User(tenant_id="homemonde", email="buyer@e2e.test", name="Test Buyer", role="manager", areas=["procurement"],
                        approval_limit=100000, password_hash=security.hash_password(PASSWORD))])
    with db.session() as s:  # no AI key: the employee writes up the verified facts only
        runtime.run(s, env.cfg, s.execute(select(WorkItem).where(WorkItem.type == "STOCKOUT_RISK")).scalar_one())
    with db.session() as s:
        item = s.execute(select(WorkItem).where(WorkItem.type == "STOCKOUT_RISK")).scalar_one()
        buy = actions.propose(s, env.cfg, item, {"action": "create_purchase_request", "summary": "Buy fabric", "rationale": "r",
                                                 "params": {"material_code": "FAB-CT-02", "quantity": 800, "unit": "m", "unit_price": 162,
                                                            "supplier": "Bagru Cotton Mills", "needed_by": "2026-12-01"}, "refs": ["E1"]})
        words = [actions.propose(s, env.cfg, item, {"action": "escalate_carrier", "summary": f"Escalate {n}", "rationale": "r",
                                                    "params": {"carrier": "Delhivery", "issue": f"late pickups {n}"}, "refs": ["E1"]})
                 for n in range(3)]
        item.next_step = f"Waiting for the AI to explain the cause ({MODEL})"  # as the server words a queued AI step
        ids = {"task": item.id, "purchase": buy.id, "wordsOnly": words[0].id, "offline": words[1].id, "stale": words[2].id}
    with db.session() as s:
        assert s.execute(select(Approval).where(Approval.status == "PENDING")).scalars().all(), "no pending suggestions"
    return ids


def test_the_web_app_survives_what_real_people_do(env, server):
    from workforce import db
    from workforce.models import ActionRecord, Approval
    ids = _setup(env)
    cfg = {"base": server, "password": PASSWORD, "owner": "owner@e2e.test", "manager": "buyer@e2e.test", "managerName": "Test Buyer",
           "model": MODEL, "nextPlain": "Waiting for the AI to explain the cause", **ids}
    run = subprocess.run([NODE, str(SCRIPT), json.dumps(cfg)], capture_output=True, text=True, timeout=240,
                         env={**os.environ, "CHROME_BIN": CHROME})
    print(run.stdout, run.stderr)
    assert run.returncode == 0, run.stdout + run.stderr
    with db.session() as s:  # what the browser did reached the server exactly as intended
        status = {a.id: a.status for a in s.execute(select(Approval)).scalars()}
        assert status[ids["purchase"]] == "PENDING"   # the bad number never left the browser
        assert status[ids["offline"]] == "PENDING"    # nothing was sent without internet
        assert status[ids["stale"]] == "APPROVED"     # approved once, by the first tab
        assert len([r for r in s.execute(select(ActionRecord)).scalars() if r.approval_id == ids["stale"]]) == 1
