"""Model access for the workforce: routing by tier, per-tenant budgets and a usage/cost ledger.

Two tiers per company (ai.strong, ai.cheap), each any provider and model. Investigations always use the strong
tier; questions, briefs and notifications use the tier set in ai.routing. Token discipline is unchanged on every
provider: stable system prompt and tool list, compact tool results, bounded steps, daily budgets."""
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import providers
from .db import utcnow
from .models import LlmUsage
from .providers import Refused, Reply, Retryable, Unavailable
from .settings import get_settings
from .tenancy import ModelRoute, Price, TenantConfig

log = logging.getLogger(__name__)

# USD per million tokens, Anthropic first-party rates (checked 2026-10). Anything else needs ai.prices in the tenant pack.
# Claude Haiku 5.5's rates apply to prompts up to 100K tokens, which the workforce's compact prompts stay well under.
BUILTIN_PRICES = {
    "claude-fable-5-1": Price(input=10, output=50, cache_read=0.25),
    "claude-opus-5-5": Price(input=4, output=20, cache_read=0.2),
    "claude-sonnet-5-5": Price(input=2, output=10, cache_read=0.2),
    "claude-haiku-5-5": Price(input=0.1, output=0.5),
    "claude-haiku-4-5": Price(input=1, output=5),
}


class LLMUnavailable(Exception):
    """AI cannot run right now (disabled, no credentials, budget spent). Work continues deterministically."""


class LLMRetryable(Exception):
    """Transient API failure: the work item is retried later (after retry_after seconds, when the provider said)."""

    def __init__(self, msg: str = "", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


# A provider that asks us to wait longer than this (a daily quota used up) is treated as unavailable for now: the
# task finishes with its verified facts instead of waiting hours, and can be explained later.
LONG_WAIT_SECONDS = 15 * 60


class LLMRefused(Exception):
    """The model declined; the item goes to a human."""


def route_label(r: ModelRoute) -> str:
    return f"{r.provider}:{r.model}"


def cost(cfg: TenantConfig, model: str, u: providers.Usage) -> float | None:
    p = cfg.ai.prices.get(model) or BUILTIN_PRICES.get(model)
    if p is None:
        return None
    read = p.cache_read if p.cache_read is not None else p.input * 0.1
    write = p.cache_write if p.cache_write is not None else p.input * 1.25
    return round((u.input_tokens * p.input + u.output_tokens * p.output + u.cache_read_tokens * read + u.cache_write_tokens * write) / 1e6, 6)


class LLM:
    def __init__(self, cfg: TenantConfig, keys: dict[str, str] | None = None):
        self.cfg = cfg
        self.keys = keys or {}  # API keys saved in the panel, by provider
        self._adapters = {}

    @classmethod
    def load(cls, db: Session, cfg: TenantConfig) -> "LLM":
        """The tenant's models as currently chosen (panel choices over the pack) with its saved keys."""
        from . import ai_settings
        return cls(ai_settings.effective(db, cfg), ai_settings.keys(db, cfg.id))

    def route(self, tier: str) -> ModelRoute:
        return getattr(self.cfg.ai, tier)

    def _adapter(self, tier: str):
        r = self.route(tier)
        if r.provider not in self._adapters:
            self._adapters[r.provider] = providers.adapter(r.provider, self.cfg.ai.providers, self.keys.get(r.provider))
        return self._adapters[r.provider]

    def unavailable_reason(self, db: Session, tier: str = "strong") -> str | None:
        if not get_settings().ai_enabled or not self.cfg.ai.enabled:
            return "AI is switched off for this deployment"
        if not self._adapter(tier).has_credentials():
            return f"no credentials configured for {self.route(tier).provider}"
        if self.cfg.ai.daily_token_budget and self.tokens_today(db) >= self.cfg.ai.daily_token_budget:
            return "daily AI token budget reached"
        if self.cfg.ai.daily_cost_budget_usd is not None and self.cost_today(db) >= self.cfg.ai.daily_cost_budget_usd:
            return "daily AI cost budget reached"
        return None

    def _since(self):
        return utcnow() - timedelta(hours=24)

    def tokens_today(self, db: Session) -> int:
        return int(db.execute(select(func.coalesce(func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens + LlmUsage.cache_write_tokens), 0))
                              .where(LlmUsage.tenant_id == self.cfg.id, LlmUsage.created_at >= self._since())).scalar())

    def cost_today(self, db: Session) -> float:
        return float(db.execute(select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0))
                                .where(LlmUsage.tenant_id == self.cfg.id, LlmUsage.created_at >= self._since())).scalar())

    def create(self, db: Session, *, tier: str, task: str, work_id: str | None, employee: str, system: str, tools: list[dict],
               messages: list, schema: dict, effort: str | None = None, final: bool = False) -> Reply:
        r = self.route(tier)
        ad = self._adapter(tier)
        try:
            reply = ad.complete(model=r.model, system=system, messages=messages, tools=tools, schema=schema,
                                effort=effort or r.effort, max_tokens=r.max_tokens, final=final,
                                fallback=self.cfg.ai.refusal_fallback)
        except (Retryable, Refused) as e:
            if getattr(e, "reply", None):
                self._record(db, e.reply, r, tier, task, work_id, employee)
            if isinstance(e, Refused):
                raise LLMRefused(str(e)) from e
            wait = e.retry_after
            if wait is not None and wait > LONG_WAIT_SECONDS:
                log.warning("%s asked to wait %.0f minutes for %s: continuing without AI", r.provider, wait / 60, work_id)
                raise LLMUnavailable(f"{r.provider} quota used up; it accepts requests again in about {wait / 3600:.1f} hours") from e
            raise LLMRetryable(str(e), wait) from e
        except Unavailable as e:
            log.error("%s rejected a request for %s: %s", r.provider, work_id, e)
            raise LLMUnavailable(str(e)) from e
        self._record(db, reply, r, tier, task, work_id, employee)
        return reply

    def _record(self, db, reply: Reply, r: ModelRoute, tier, task, work_id, employee):
        u = reply.usage
        db.add(LlmUsage(tenant_id=self.cfg.id, work_id=work_id, employee=employee, task=task, tier=tier, provider=r.provider,
                        model=reply.model or r.model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                        cache_read_tokens=u.cache_read_tokens, cache_write_tokens=u.cache_write_tokens,
                        cost_usd=cost(self.cfg, r.model, u)))
        db.flush()


def assistant_message(reply: Reply) -> dict:
    """Neutral assistant turn; the raw provider content rides along so the same model can replay it exactly."""
    msg = {"role": "assistant", "content": reply.blocks}
    if reply.raw is not None:
        msg["_raw"] = reply.raw
    return msg
