"""Owners choose the AI provider and model for each tier and enter API keys from the panel."""
import json

import pytest
from sqlalchemy import select

from conftest import stockout_scenario, text, use_fake
from test_workforce import _item, _login, _sync_and_rules

KEY = "sk-deepseek-test-1234567890abcd"


@pytest.fixture
def secret(monkeypatch):
    from workforce import settings
    monkeypatch.setenv("WORKFORCE_SECRET_KEY", "a-long-random-deployment-secret")
    settings.get_settings.cache_clear()


def _manager(s):
    from workforce import security
    from workforce.models import User
    s.add(User(tenant_id="homemonde", email="wh@homemonde.in", name="Warehouse Head", role="manager", areas=["warehouse"],
               password_hash=security.hash_password("correct horse battery")))


def test_owner_switches_the_careful_ai_to_deepseek_with_a_key_from_the_panel(env, client, secret, monkeypatch):
    from workforce import db, providers, runtime, work
    from workforce.models import Event, LlmUsage, ProviderKey, WorkItem
    with db.session() as s:
        _manager(s)
    h = {"X-CSRF-Token": _login(client, "wh@homemonde.in")}
    assert client.get("/api/ai/settings").status_code == 403
    assert client.post("/api/ai/keys/deepseek", json={"key": KEY}, headers=h).status_code == 403
    h = {"X-CSRF-Token": _login(client, "founder@homemonde.in")}
    assert client.post("/api/ai/keys/deepseek", json={"key": KEY}, headers=h).status_code == 200
    body = {"tier": "strong", "provider": "deepseek", "model": "deepseek-reasoner", "effort": "medium", "price_input": 0.5, "price_output": 2}
    assert client.post("/api/ai/route", json=body, headers=h).status_code == 200
    view = client.get("/api/ai/settings")
    assert KEY not in view.text  # the key is never sent back
    ov = view.json()
    assert ov["tiers"]["strong"]["provider"] == "deepseek" and ov["tiers"]["strong"]["changed_in_panel"]
    assert ov["tiers"]["strong"]["default"]["provider"] == "anthropic"
    ds = next(p for p in ov["providers"] if p["id"] == "deepseek")
    assert ds["key"] == "panel" and ds["hint"] == KEY[-4:] and ds["label"] == "DeepSeek"
    with db.session() as s:
        assert KEY not in s.get(ProviderKey, ("homemonde", "deepseek")).secret  # stored encrypted
        assert all(KEY not in e.msg and KEY not in json.dumps(e.payload) for e in s.execute(select(Event)).scalars())

    # the next investigation runs on DeepSeek with the panel key, and its cost uses the price given
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    sent = []
    finding = {"outcome": "FINDING", "headline": "HM-204 is running low", "evidence_quality": "LIKELY", "impact": "", "unknowns": [],
               "statements": [{"kind": "FACT", "text": "Sellable stock is 280 units.", "refs": ["M1"]}],
               "recommendation": {"action": "none", "summary": "", "rationale": "", "params": [], "refs": []}}

    def post(url, headers, body, timeout):
        sent.append((url, headers, body))
        return {"model": "deepseek-reasoner", "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(finding)}}]}

    monkeypatch.setattr(providers.OpenAICompatAdapter, "post", staticmethod(post))
    with db.session() as s:
        wid = work.claim(s, ["homemonde"], 600).id
    with db.session() as s:
        runtime.run(s, env.cfg, s.get(WorkItem, ("homemonde", wid)))
    url, headers, sent_body = sent[0]
    assert url == "https://api.deepseek.com/chat/completions" and headers["Authorization"] == f"Bearer {KEY}"
    assert sent_body["model"] == "deepseek-reasoner"
    assert _item(wid).finding["model"] == "deepseek:deepseek-reasoner"
    with db.session() as s:
        use = s.execute(select(LlmUsage).where(LlmUsage.work_id == wid)).scalars().first()
        assert (use.provider, use.model) == ("deepseek", "deepseek-reasoner")
        assert use.cost_usd == pytest.approx((1000 * 0.5 + 100 * 2) / 1e6)

    # back to the company default, and the key can be removed
    assert client.post("/api/ai/route/reset", json={"tier": "strong"}, headers=h).status_code == 200
    assert client.post("/api/ai/keys/deepseek/remove", headers=h).status_code == 200
    ov = client.get("/api/ai/settings").json()
    assert ov["tiers"]["strong"]["provider"] == "anthropic" and not ov["tiers"]["strong"]["changed_in_panel"]
    assert next(p for p in ov["providers"] if p["id"] == "deepseek")["key"] == "none"
    with db.session() as s:
        types = {e.type for e in s.execute(select(Event).where(Event.category == "security")).scalars()}
    assert {"AI_KEY_SET", "AI_SETTINGS_CHANGED", "AI_KEY_REMOVED"} <= types


def test_choices_are_checked_before_saving(env, client, secret):
    h = {"X-CSRF-Token": _login(client, "founder@homemonde.in")}
    base = {"tier": "cheap", "provider": "openai", "model": "gpt-model", "effort": "low"}
    assert client.post("/api/ai/route", json={**base, "provider": "nobody"}, headers=h).status_code == 400
    assert client.post("/api/ai/route", json={**base, "model": "bad model name!"}, headers=h).status_code == 400
    assert client.post("/api/ai/route", json={**base, "price_input": 1}, headers=h).status_code == 400  # both prices or neither
    assert client.post("/api/ai/route", json={**base, "tier": "middle"}, headers=h).status_code == 422
    assert client.post("/api/ai/keys/openai", json={"key": "has a space in it"}, headers=h).status_code == 400
    assert client.post("/api/ai/route", json=base, headers=h).status_code == 200


def test_keys_need_the_deployment_secret_and_a_changed_secret_hides_old_keys(env, client, monkeypatch):
    from workforce import ai_settings, db, settings
    h = {"X-CSRF-Token": _login(client, "founder@homemonde.in")}
    assert client.get("/api/ai/settings").json()["can_store_keys"] is False
    assert client.post("/api/ai/keys/openai", json={"key": KEY}, headers=h).status_code == 409
    monkeypatch.setenv("WORKFORCE_SECRET_KEY", "first-secret")
    settings.get_settings.cache_clear()
    assert client.post("/api/ai/keys/openai", json={"key": KEY}, headers=h).status_code == 200
    with db.session() as s:
        assert ai_settings.keys(s, "homemonde") == {"openai": KEY}
    monkeypatch.setenv("WORKFORCE_SECRET_KEY", "rotated-secret")
    settings.get_settings.cache_clear()
    with db.session() as s:
        assert ai_settings.keys(s, "homemonde") == {}  # cannot be read: treated as missing, never as garbage
    openai = next(p for p in client.get("/api/ai/settings").json()["providers"] if p["id"] == "openai")
    assert openai["unreadable"] and openai["key"] == "none"


def test_connection_test_reports_plainly(env, client, secret):
    h = {"X-CSRF-Token": _login(client, "founder@homemonde.in")}
    r = client.post("/api/ai/test", json={"tier": "cheap"}, headers=h).json()
    assert r == {"ok": False, "model": "anthropic:claude-haiku-5-5", "message": "no credentials configured for anthropic"}
    use_fake([lambda kw: [text({"ok": True})]])
    r = client.post("/api/ai/test", json={"tier": "strong"}, headers=h).json()
    assert r["ok"] and r["model"] == "anthropic:claude-sonnet-5-5"
