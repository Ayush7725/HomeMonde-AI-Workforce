"""AI spend controls: the most capable model only where it pays off, figures-first alerts with an Explain button,
unchanged repeats that reuse the earlier explanation, and batched alerts investigated together."""
from datetime import timedelta

from sqlalchemy import select

from conftest import stockout_scenario, text, use_fake, write_csv
from test_workforce import _item, _login, _stockout_script, _sync_and_rules


def _run(env, cfg) -> str:
    from workforce import db, runtime, work
    from workforce.models import WorkItem
    with db.session() as s:
        item = work.claim(s, ["homemonde"], 600)
        assert item is not None, "expected a queued work item"
        wid = item.id
    with db.session() as s:
        runtime.run(s, cfg, s.get(WorkItem, ("homemonde", wid)))
    return wid


def _ai(env, **changes):
    return env.cfg.model_copy(update={"ai": env.cfg.ai.model_copy(update=changes)})


def test_the_pack_runs_sonnet_by_default_and_opus_only_for_critical_alerts(env):
    assert env.cfg.ai.strong.model == "claude-sonnet-5-5" and env.cfg.ai.cheap.model == "claude-haiku-5-5"
    assert env.cfg.ai.investigation_tier("HIGH") == "strong" and env.cfg.ai.investigation_tier("CRITICAL") == "premium"
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    fake = use_fake(_stockout_script())
    wid = _run(env, _ai(env, premium_for=["HIGH"]))  # treat this HIGH alert as one that deserves the premium model
    assert {c["model"] for c in fake.calls} == {"claude-opus-5-5"}
    assert _item(wid).finding["model"] == "anthropic:claude-opus-5-5"
    from workforce import db
    from workforce.models import LlmUsage
    with db.session() as s:
        assert {r.tier for r in s.execute(select(LlmUsage).where(LlmUsage.work_id == wid)).scalars()} == {"premium"}


def test_alerts_below_auto_explain_cost_nothing_until_someone_asks(env, client):
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    fake = use_fake([])  # any model call would fail the test
    cfg = _ai(env, auto_explain=["CRITICAL"])
    wid = _run(env, cfg)
    it = _item(wid)
    assert fake.calls == [] and it.status == "COMPLETED" and it.finding["ai"] is False and it.finding["explain_available"]
    assert any("Stock coverage" in s["text"] for s in it.finding["statements"])  # the verified figures are still reported
    csrf = _login(client, "founder@homemonde.in")
    assert client.post(f"/api/work/{wid}/explain", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert _item(wid).status == "QUEUED"
    assert client.post(f"/api/work/{wid}/explain", headers={"X-CSRF-Token": csrf}).status_code == 409  # already asked
    fake = use_fake(_stockout_script())
    assert _run(env, cfg) == wid
    it = _item(wid)
    assert fake.calls and it.finding["ai"] is True and it.status == "NEEDS_HUMAN_REVIEW"  # now explained, with a suggestion


def test_an_unchanged_repeat_reuses_the_earlier_explanation(env):
    from workforce import actions, db, rules
    from workforce.models import Approval, User, WorkItem
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    first = _run(env, env.cfg)
    with db.session() as s:  # the team decides on the first alert, then it ages past the cooldown
        owner = User(tenant_id="homemonde", email="o@x.in", name="Founder", role="owner", password_hash="x")
        s.add(owner)
        s.flush()
        ap = s.execute(select(Approval).where(Approval.work_id == first)).scalar_one()
        actions.decide(s, env.cfg, ap.id, owner, "REJECTED", reason="buying elsewhere")
        s.get(WorkItem, ("homemonde", first)).created_at = db.utcnow() - timedelta(hours=25)
    with db.session() as s:
        assert rules.evaluate(s, env.cfg) == 1
    fake = use_fake([])
    again = _run(env, env.cfg)
    it = _item(again)
    assert fake.calls == [] and it.finding["repeat_of"] == first and it.finding["explain_available"]
    assert any(first in u for u in it.finding["unknowns"])


def test_a_changed_repeat_is_investigated_afresh(env):
    from workforce import db, rules
    from workforce.models import WorkItem
    stockout_scenario(env.exports, env.cfg)
    _sync_and_rules(env)
    use_fake(_stockout_script())
    first = _run(env, env.cfg)
    with db.session() as s:
        it = s.get(WorkItem, ("homemonde", first))
        it.status, it.created_at = "COMPLETED", db.utcnow() - timedelta(hours=25)
        pack = dict(it.context["evidence_pack"])
        pack["sellable_units"] = {**pack["sellable_units"], "value": pack["sellable_units"]["value"] * 2}  # the situation moved
        it.context = {**it.context, "evidence_pack": pack}
    with db.session() as s:
        assert rules.evaluate(s, env.cfg) == 1
    fake = use_fake(_stockout_script())
    _run(env, env.cfg)
    assert fake.calls  # a real change gets a real investigation


def test_every_sku_at_risk_in_one_pass_is_one_investigation(env):
    from workforce import db, quality, work
    from workforce.models import WorkItem
    stockout_scenario(env.exports, env.cfg)
    write_csv(env.exports / "inventory.csv", [  # HM-118 sells 10 a day and now has 30 left: also at risk
        {"sku": "HM-204", "location": "JAIPUR-WH", "product_name": "Indigo block-print cushion cover", "available": 320, "reserved": 40, "incoming": 0},
        {"sku": "HM-118", "location": "JAIPUR-WH", "product_name": "Linen table runner", "available": 30, "reserved": 0, "incoming": 0}])
    _, created = _sync_and_rules(env)
    assert created == 1
    with db.session() as s:
        item = s.execute(select(WorkItem).where(WorkItem.rule_id == "stockout_risk")).scalar_one()
        assert work.item_groups(item.context) == ["HM-118", "HM-204"] and item.title == "Stockout risk · HM-118 and 1 more"
    with db.session() as s:  # each SKU keeps its own cooldown: nothing new on the next pass
        assert work.groups_covered(s, "homemonde", "stockout_risk", 24) == {"HM-118", "HM-204"}
    from workforce import rules
    with db.session() as s:
        assert rules.evaluate(s, env.cfg) == 0
    fake = use_fake([lambda kw: [text({"outcome": "INSUFFICIENT_EVIDENCE", "headline": "", "statements": [],
                                       "evidence_quality": "INSUFFICIENT_EVIDENCE", "impact": "", "unknowns": [],
                                       "recommendation": {"action": "none", "summary": "", "rationale": "", "params": [], "refs": []}})]])
    wid = _run(env, env.cfg.model_copy(update={"ai": env.cfg.ai.model_copy(update={"auto_explain": []})}))
    statements = _item(wid).finding["statements"]
    assert fake.calls == []
    assert any(s["text"].startswith("HM-118: ") for s in statements) and any(s["text"].startswith("HM-204: ") for s in statements)
    with db.session() as s:  # the seeded evidence covers both SKUs, each with its own refs
        from workforce.models import Evidence
        groups = {e.args.get("sku") for e in s.execute(select(Evidence).where(Evidence.work_id == wid)).scalars()}
        assert groups == {"HM-118", "HM-204"}
    assert quality.load_suite("homemonde")  # the eval suite still reads the batched pack
