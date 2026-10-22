"""Model routing, the multi-provider layer, rewording checks, pause/kill, health, reports, notifications,
reviews and the AI-quality suite."""
import json

import pytest
from sqlalchemy import select

from conftest import stockout_scenario, text, tool_use, use_fake
from test_workforce import _item, _login, _run_next, _sync_and_rules

NONE_REC = {"action": "none", "summary": "", "rationale": "", "params": [], "refs": []}


def _answer(outcome="FINDING", statements=None, **kw):
    return {"outcome": outcome, "headline": kw.get("headline", ""), "statements": statements or [], "evidence_quality": kw.get("quality", "CONFIRMED"),
            "impact": "", "unknowns": [], "recommendation": kw.get("recommendation", NONE_REC)}


def _ask(env, question):
    from workforce import db, work
    with db.session() as s:
        return work.create(s, env.cfg, employee=env.cfg.question_employee(), type_="MANAGEMENT_QUESTION", title=question,
                           subject=question, origin="question", context={"question": question}).id


def _events(type_):
    from workforce import db
    from workforce.models import Event
    with db.session() as s:
        return [e.msg for e in s.execute(select(Event).where(Event.type == type_)).scalars()]


# ---------------------------------------------------------------- routing

def test_lookup_question_runs_on_the_cheap_model(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    from workforce import db, work
    with db.session() as s:  # keep the rule's stockout item out of the way
        for it in s.execute(select(work.WorkItem)).scalars():
            it.status = "COMPLETED"
    wid = _ask(env, "What is the stock coverage of HM-204?")
    fake = use_fake([
        lambda kw: [tool_use("get_stock_coverage", {"sku": "HM-204"}, 1)],
        lambda kw: [text(_answer(statements=[{"kind": "FACT", "text": "HM-204 has 2.9474 days of cover.", "refs": ["E1"]}]))],
    ])
    assert _run_next(env) == wid
    assert {c["model"] for c in fake.calls} == {"claude-haiku-5-5"}
    assert fake.calls[0]["output_config"]["format"]["schema"]["properties"]["recommendation"]["properties"]["action"]["enum"] == ["none"]
    item = _item(wid)
    assert item.outcome == "FINDING" and item.finding["model"] == "anthropic:claude-haiku-5-5"
    assert not _events("ROUTE_ESCALATED")


def test_cheap_answer_that_fails_the_evidence_check_is_redone_on_the_strong_model(env):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    from workforce import db, work
    with db.session() as s:
        for it in s.execute(select(work.WorkItem)).scalars():
            it.status = "COMPLETED"
    wid = _ask(env, "What is the stock coverage of HM-204?")
    good = [{"kind": "FACT", "text": "HM-204 has 2.9474 days of cover.", "refs": ["E1"]}]
    fake = use_fake([
        lambda kw: [tool_use("get_stock_coverage", {"sku": "HM-204"}, 1)],
        lambda kw: [text(_answer(statements=[{"kind": "FACT", "text": "HM-204 has 12 days of cover.", "refs": ["E1"]}]))],
        lambda kw: [text(_answer(statements=good))],  # strong model, reusing E1 from the cheap round
    ])
    _run_next(env)
    assert [c["model"] for c in fake.calls] == ["claude-haiku-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"]
    assert "evidence_from_previous_round" in fake.calls[2]["messages"][0]["content"]
    item = _item(wid)
    assert item.finding["statements"] == good and item.finding["model"] == "anthropic:claude-sonnet-5-5"
    assert any("failed the evidence check" in m for m in _events("ROUTE_ESCALATED"))


def test_cheap_model_hands_analysis_questions_to_the_strong_model(env):
    wid = _ask(env, "Why did profit fall last month?")
    fake = use_fake([lambda kw: [text(_answer("NEEDS_INVESTIGATION"))],
                     lambda kw: [text(_answer("INSUFFICIENT_EVIDENCE", quality="INSUFFICIENT_EVIDENCE"))]])
    _run_next(env)
    assert [c["model"] for c in fake.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert _item(wid).outcome == "INSUFFICIENT_EVIDENCE"
    assert any("needs analysis" in m for m in _events("ROUTE_ESCALATED"))


# ---------------------------------------------------------------- rewording

def test_reworded_lines_with_new_numbers_are_rejected_and_retried_on_the_strong_model(env):
    from workforce import db, wording
    items = [{"id": "a", "text": "Sellable stock is 280 units with 2.9 days of cover."},
             {"id": "b", "text": "Supplier PO-7731 is 8 days late."}]

    def reword(its, model):
        if model == "claude-haiku-5-5":  # cheap model invents a number in one line
            return {"lines": [{"id": "a", "text": "Only 280 units left, about 3 days."}, {"id": "b", "text": "PO-7731 is running 8 days late."}]}
        return {"lines": [{"id": "a", "text": "280 units remain, 2.9 days of cover."}]}  # strong model drops line b

    fake = use_fake([], reword)
    with db.session() as s:
        out = wording.reword(s, env.cfg, items, "brief")
    assert [c["model"] for c in fake.reword_calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert out["lines"]["a"] == "280 units remain, 2.9 days of cover."
    assert out["lines"]["b"] == "PO-7731 is running 8 days late."  # the cheap line that passed is kept
    assert out["rejected"] == 0 and out["model"] == "anthropic:claude-haiku-5-5 then anthropic:claude-sonnet-5-5"


def test_rewording_without_credentials_keeps_verified_text(env):
    from workforce import db, wording
    with db.session() as s:
        out = wording.reword(s, env.cfg, [{"id": "a", "text": "Stock is 280 units."}], "notification")
    assert out == {"lines": {"a": "Stock is 280 units."}, "overview": None, "model": None, "rejected": 0, "fallback": True}


# ---------------------------------------------------------------- providers

def test_openai_compatible_provider_with_tools_usage_and_cost(env, monkeypatch):
    """Any OpenAI-compatible service (OpenAI, Gemini, DeepSeek, Hugging Face...) runs the same investigation loop."""
    from workforce import db, notify, providers, quality, runtime, work
    from workforce.models import LlmUsage, WorkItem
    from workforce.tenancy import ModelRoute, Price, ProviderConfig
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    ai = env.cfg.ai.model_copy(update={
        "strong": ModelRoute(provider="lab", model="lab-large"), "cheap": ModelRoute(provider="deepseek", model="deepseek-chat"),
        "providers": {"lab": ProviderConfig(base_url="https://llm.example.com/v1", api_key_env="LAB_KEY", structured="json_schema")},
        "prices": {"lab-large": Price(input=1, output=2)}})
    cfg = env.cfg.model_copy(update={"ai": ai})
    monkeypatch.setenv("LAB_KEY", "secret-lab")
    sent = []
    finding = _answer(statements=[{"kind": "FACT", "text": "320 units are available and 40 are reserved.", "refs": ["E1"]}], quality="LIKELY")

    def post(url, headers, body, timeout):
        sent.append((url, headers, body))
        if "deepseek" in url:  # notification rewording on the cheap tier
            items = json.loads(body["messages"][1]["content"])["items"]
            return {"model": "deepseek-chat", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"lines": items})}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        if len([u for u, *_ in sent if "llm.example.com" in u]) == 1:
            return {"model": "lab-large", "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 400}},
                    "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "get_inventory_position", "arguments": '{"sku": "HM-204"}'}}]}}]}
        return {"model": "lab-large", "usage": {"prompt_tokens": 1500, "completion_tokens": 300},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(finding)}}]}

    monkeypatch.setattr(providers.OpenAICompatAdapter, "post", staticmethod(post))
    with db.session() as s:
        item = work.claim(s, ["homemonde"], 600)
        wid = item.id
    with db.session() as s:
        runtime.run(s, cfg, s.get(WorkItem, ("homemonde", wid)))
    notify.deliver_pending(cfg)  # the worker rewords notifications after the investigation has committed
    lab = [x for x in sent if "llm.example.com" in x[0]]
    url, headers, first = lab[0]
    assert url == "https://llm.example.com/v1/chat/completions" and headers["Authorization"] == "Bearer secret-lab"
    assert first["response_format"]["type"] == "json_schema" and first["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in first["tools"]} >= {"get_inventory_position", "calculate"}
    second = lab[1][2]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "get_inventory_position"
    assert second[-1]["role"] == "tool" and json.loads(second[-1]["content"])["ref"] == "E1"
    assert _item(wid).finding["statements"][0]["text"] == "320 units are available and 40 are reserved."
    with db.session() as s:
        rows = s.execute(select(LlmUsage).where(LlmUsage.work_id == wid)).scalars().all()
        assert [(r.provider, r.model, r.tier) for r in rows] == [("lab", "lab-large", "strong")] * 2
        assert rows[0].input_tokens == 600 and rows[0].cache_read_tokens == 400
        assert rows[0].cost_usd == pytest.approx((600 * 1 + 400 * 0.1 + 50 * 2) / 1e6)
        cheap = s.execute(select(LlmUsage).where(LlmUsage.provider == "deepseek")).scalars().all()
        assert cheap and all(r.cost_usd is None and r.task == "notification" for r in cheap)  # no price configured: cost unknown, not guessed
        assert quality.accuracy(s, "homemonde") == {}


def test_provider_errors_map_to_retry_and_unavailable(env, monkeypatch):
    import urllib.error

    from workforce import providers
    from workforce.tenancy import ProviderConfig
    ad = providers.OpenAICompatAdapter("x", ProviderConfig(base_url="https://x.example/v1"))
    for code, exc in ((429, providers.Retryable), (503, providers.Retryable), (401, providers.Unavailable)):
        def post(*a, code=code):
            raise urllib.error.HTTPError("u", code, "err", {}, None)
        monkeypatch.setattr(providers.OpenAICompatAdapter, "post", staticmethod(post))
        with pytest.raises(exc):
            ad.complete(model="m", system="s", messages=[{"role": "user", "content": "q"}], tools=[], schema={}, effort="low",
                        max_tokens=100, final=False, fallback=False)
    monkeypatch.setattr(providers.OpenAICompatAdapter, "post", staticmethod(
        lambda *a: {"choices": [{"finish_reason": "stop", "message": {"refusal": "no", "content": None}}], "usage": {}}))
    with pytest.raises(providers.Refused):
        ad.complete(model="m", system="s", messages=[{"role": "user", "content": "q"}], tools=[], schema={}, effort="low",
                    max_tokens=100, final=False, fallback=False)


def test_routes_come_from_config_or_environment(monkeypatch):
    from workforce.tenancy import AiConfig, ModelRoute
    monkeypatch.setenv("ACME_CHEAP", "gemini:gemini-flash-test:low")
    r = ModelRoute.model_validate("${ACME_CHEAP:-anthropic:claude-haiku-4-5}")
    assert (r.provider, r.model, r.effort) == ("gemini", "gemini-flash-test", "low")
    assert ModelRoute.model_validate("${UNSET_VAR_X:-openai:some-model}").provider == "openai"
    with pytest.raises(ValueError):
        AiConfig(strong="nowhere:model")
    with pytest.raises(ValueError):
        AiConfig(routing={"investigation": "cheap"})


# ---------------------------------------------------------------- pause / kill, health

def test_pause_stops_new_work_colleague_requests_and_actions(env):
    from workforce import db, health, security, tools, work
    from workforce.metrics import MetricEngine
    from workforce.models import User
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    with db.session() as s:
        owner = User(tenant_id="homemonde", email="o@x.in", name="Founder", role="owner", password_hash=security.hash_password("correct horse battery"))
        s.add(owner)
        s.flush()
        health.set_paused(s, env.cfg, "employee", "inventory_ai", True, owner, "checking a bad recommendation")
        health.set_paused(s, env.cfg, "employee", "sales_ai", True, owner, "")
    with db.session() as s:
        assert work.claim(s, ["homemonde"], 600) is None  # the only queued item belongs to a paused employee
        h = health.employee_health(s, env.cfg, {"inventory_ai", "procurement_ai"})
        assert h["inventory_ai"]["status"] == "PAUSED" and "checking a bad recommendation" in h["inventory_ai"]["reasons"][0]
        assert h["procurement_ai"]["status"] == "HEALTHY"
        item = s.execute(select(work.WorkItem)).scalar_one()
        ctx = tools.ToolContext(db=s, cfg=env.cfg, root=item, employee="inventory_ai", engine=MetricEngine(s, env.cfg))
        with pytest.raises(tools.ToolError, match="paused"):
            tools.call(ctx, "ask_sales_ai__get_sales_velocity", {"sku": "HM-204"})
        owner = s.execute(select(User)).scalar_one()
        health.set_paused(s, env.cfg, "employee", "inventory_ai", False, owner)
    with db.session() as s:
        assert work.claim(s, ["homemonde"], 600) is not None
    assert any("paused Inventory AI" in m for m in _events("EMPLOYEE_PAUSED"))


def test_pause_api_is_owner_only_and_shows_in_state(env, client):
    from workforce import db, security
    from workforce.models import User
    with db.session() as s:
        s.add(User(tenant_id="homemonde", email="m@homemonde.in", name="Ops", role="manager", areas=["warehouse"],
                   password_hash=security.hash_password("correct horse battery")))
    csrf = _login(client, "m@homemonde.in")
    assert client.post("/api/employees/inventory_ai/pause", json={"reason": "x"}, headers={"X-CSRF-Token": csrf}).status_code == 403
    csrf = _login(client, "founder@homemonde.in")
    assert client.post("/api/employees/inventory_ai/pause", json={"reason": "audit"}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/state").json()["employees"]["inventory_ai"]["paused"] is True
    h = client.get("/api/workforce/health").json()["employees"]
    assert h["inventory_ai"]["status"] == "PAUSED" and set(h) == set(env.cfg.employees)


def test_sensitive_employee_is_hidden_from_other_managers(env, client):
    from workforce import db, security
    from workforce.models import User
    with db.session() as s:
        s.add(User(tenant_id="homemonde", email="factory@homemonde.in", name="Factory Head", role="manager", areas=["factory"],
                   password_hash=security.hash_password("correct horse battery")))
    _login(client, "factory@homemonde.in")
    boot = client.get("/api/bootstrap").json()
    assert "hr_ai" not in boot["employees"] and "manufacturing_ai" in boot["employees"]
    assert "hr_ai" not in client.get("/api/workforce/health").json()["employees"]
    assert "hr_ai" not in env.cfg.employees["executive_ai"].can_ask


# ---------------------------------------------------------------- reports, notifications, reviews

def _finish_stockout_with_recommendation(env):
    from test_workforce import _stockout_script
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    fake = use_fake(_stockout_script(), reword=lambda items, model: {"lines": [{"id": i["id"], "text": i["text"]} for i in items],
                                                                      "overview": items[0]["text"]})
    return _run_next(env), fake


def test_high_priority_finding_notifies_and_lands_in_the_daily_brief(env, client):
    wid, fake = _finish_stockout_with_recommendation(env)
    from workforce import db, reports
    from workforce.models import Notification
    with db.session() as s:
        kinds = sorted(n.kind for n in s.execute(select(Notification)).scalars())
        assert kinds == ["decision", "finding"]
        r = reports.generate(s, env.cfg, "daily")
        sections = {sec["key"]: sec["items"] for sec in r.content["sections"]}
        assert [i["work_id"] for i in sections["attention"]] == [wid]
        assert sections["waiting"][0]["verified"].startswith("Waiting for a decision")
        assert any(c["system"][0]["text"].startswith("You rewrite") for c in fake.reword_calls)
        assert r.content["wording"]["rejected_lines"] == 0
        rid = r.id
    csrf = _login(client, "founder@homemonde.in")
    assert client.get("/api/state").json()["unread_notifications"] == 2
    client.post("/api/notifications/seen", headers={"X-CSRF-Token": csrf})
    assert client.get("/api/state").json()["unread_notifications"] == 0
    rep = client.get(f"/api/reports/{rid}").json()
    assert rep["overview"] and rep["ai_usage"]
    _login(client, "sales.head@homemonde.in")  # manager of sales: no inventory items, no overview, no AI spend
    rep = client.get(f"/api/reports/{rid}").json()
    assert "overview" not in rep and "ai_usage" not in rep
    assert all(i["employee"] in (None, "sales_ai") for sec in rep["sections"] for i in sec["items"])


def test_reports_are_due_once_per_period(env):
    from workforce import db, reports
    with db.session() as s:
        due = reports.due(s, env.cfg)
        for k in due:
            reports.generate(s, env.cfg, k)
    with db.session() as s:
        assert reports.due(s, env.cfg) == []


def test_reviews_give_an_accuracy_score_and_outcome_checks_run_when_due(env):
    from datetime import timedelta

    from workforce import db, quality, security
    from workforce.db import utcnow
    from workforce.models import FindingReview, User, WorkItem
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    wid = _run_next(env)  # no AI: verified-facts finding
    with db.session() as s:
        item = s.get(WorkItem, ("homemonde", wid))
        item.created_at = utcnow() - timedelta(days=6)  # the stockout rule checks its prediction after 5 days
        assert quality.check_outcomes(s, env.cfg) == 1
        auto = s.execute(select(FindingReview)).scalar_one()
        assert auto.source == "auto" and auto.verdict == "WRONG"  # no action taken and cover is still 2.9 days, not under 2
        assert quality.accuracy(s, "homemonde")["inventory_ai"]["score"] == 0.0
        owner = User(tenant_id="homemonde", email="o@x.in", name="Founder", role="owner", password_hash=security.hash_password("correct horse battery"))
        s.add(owner)
        s.flush()
        quality.review(s, env.cfg, wid, owner, "CORRECT", "the risk was real; we expedited outside the system")
        acc = quality.accuracy(s, "homemonde")["inventory_ai"]
        assert acc == {"correct": 1, "partly": 0, "wrong": 0, "reviewed": 1, "score": 1.0}  # a person's verdict overrides the check
        assert quality.check_outcomes(s, env.cfg) == 0


def test_eval_suite_runs_in_isolation_and_records_results(env):
    from workforce import db, quality
    from workforce.models import EvalResult, WorkItem
    run_id, results = quality.run_suite("homemonde")
    by = {r.scenario: r for r in results}
    assert set(by) == {s["id"] for s in quality.load_suite("homemonde")}
    assert by["healthy_stock_raises_nothing"].status == "PASSED"
    for sid in ("stockout_with_blocked_production", "profit_drop_traced_across_departments", "late_courier"):
        rule_check = next(c for c in by[sid].checks if c["check"] == "rules created the expected work")
        assert rule_check["result"] == "pass", (sid, rule_check)
        assert by[sid].status == "SKIPPED"  # no credentials: AI checks are skipped, never faked
    with db.session() as s:
        assert s.execute(select(WorkItem)).first() is None  # scenarios never touch the real database
        assert len(s.execute(select(EvalResult).where(EvalResult.run_id == run_id)).scalars().all()) == len(results)
        cmp = quality.comparison(s, "homemonde")
        assert cmp[0]["strong"] == "anthropic:claude-sonnet-5-5" and cmp[0]["scenarios"] == len(results)


def test_provider_data_on_a_tool_call_goes_back_to_the_same_model(monkeypatch):
    """Gemini 3 refuses the next turn (400) unless each tool call's thought_signature is sent back with it."""
    from workforce import providers
    from workforce.llm import assistant_message
    from workforce.tenancy import ProviderConfig
    sent = []

    def post(url, headers, body, timeout):
        sent.append(body)
        if len(sent) == 1:
            return {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_stock", "arguments": "{}"},
                 "extra_content": {"google": {"thought_signature": "sig-1"}}}]}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    monkeypatch.setattr(providers.OpenAICompatAdapter, "post", staticmethod(post))
    ad = providers.OpenAICompatAdapter("g", ProviderConfig(base_url="https://g.example/v1"), api_key="k")
    kw = dict(system="s", tools=[], schema={}, effort=None, max_tokens=100, final=False, fallback=None)
    first = ad.complete(model="m1", messages=[{"role": "user", "content": "q"}], **kw)
    history = [{"role": "user", "content": "q"}, assistant_message(first),
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "280"}]}]
    assert first.blocks == [{"type": "tool_use", "id": "c1", "name": "get_stock", "input": {}}]  # neutral blocks stay clean
    ad.complete(model="m1", messages=history, **kw)
    assert sent[1]["messages"][2]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig-1"}}
    ad.complete(model="other-model", messages=history, **kw)  # another model never receives it
    assert "extra_content" not in sent[2]["messages"][2]["tool_calls"][0]


def _rate_limited_run(env, monkeypatch, delay: str):
    """Run the stockout investigation on an OpenAI-compatible provider that answers every call with a 429."""
    import io
    import urllib.error

    from workforce import db, providers, runtime, work
    from workforce.models import WorkItem
    from workforce.tenancy import ModelRoute, ProviderConfig
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    ai = env.cfg.ai.model_copy(update={"strong": ModelRoute(provider="lab", model="lab-large"),
                                       "providers": {"lab": ProviderConfig(base_url="https://llm.example.com/v1", api_key_env="LAB_KEY")}})
    cfg = env.cfg.model_copy(update={"ai": ai})
    monkeypatch.setenv("LAB_KEY", "k")
    body = json.dumps([{"error": {"code": 429, "details": [{"retryDelay": delay}]}}]).encode()

    def post(*a):
        raise urllib.error.HTTPError("u", 429, "quota", {}, io.BytesIO(body))
    monkeypatch.setattr(providers.OpenAICompatAdapter, "post", staticmethod(post))
    with db.session() as s:
        item = work.claim(s, ["homemonde"], 600)
        wid, attempts = item.id, item.attempts
    with db.session() as s:
        runtime.run(s, cfg, s.get(WorkItem, ("homemonde", wid)))
    with db.session() as s:
        it = s.get(WorkItem, ("homemonde", wid))
        return it.status, it.attempts, attempts, it.next_attempt_at, it.finding


def test_a_short_rate_limit_waits_as_asked_without_using_an_attempt(env, monkeypatch):
    from workforce.db import utcnow
    status, attempts, before, at, _ = _rate_limited_run(env, monkeypatch, "40s")
    assert status == "NEEDS_RETRY" and attempts == before - 1
    assert 30 < (at - utcnow()).total_seconds() <= 40


def test_a_used_up_daily_quota_finishes_with_verified_facts_instead_of_waiting_hours(env, monkeypatch):
    status, _, _, _, finding = _rate_limited_run(env, monkeypatch, "7706s")
    assert status in ("COMPLETED", "NEEDS_HUMAN_REVIEW") and finding["ai"] is False
    assert any("quota used up" in u for u in finding["unknowns"]) or "quota used up" in json.dumps(finding)
