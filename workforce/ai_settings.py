"""Model choice and API keys managed by owners from the panel.

The tenant pack sets each company's default models. An owner may choose another provider and model for either
tier (strong or cheap) and enter that provider's API key. Keys are encrypted with the deployment's secret key
(WORKFORCE_SECRET_KEY), never returned to a browser, never logged and never written to the event log; only the
last four characters are kept in clear so people can recognise which key is in use. A key in the environment
still works and is used when no panel key is saved."""
import base64
import hashlib
import re
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import events
from .db import utcnow
from .models import AiRoute, ProviderKey, User
from .settings import get_settings
from .tenancy import BUILTIN_PROVIDERS, ModelRoute, Price, TenantConfig

TIERS = ("strong", "cheap")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}$")
PROVIDER_LABELS = {"anthropic": "Claude (Anthropic)", "openai": "ChatGPT (OpenAI)", "gemini": "Gemini (Google)",
                   "deepseek": "DeepSeek", "huggingface": "Hugging Face"}
_preset: dict[str, tuple[dict, dict]] = {}  # tenant -> (routes, keys) for runs outside the main database


class SettingsError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _fernet() -> Fernet | None:
    secret = get_settings().secret_key
    if not secret:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def can_store_keys() -> bool:
    return _fernet() is not None


def providers(cfg: TenantConfig) -> list[str]:
    return sorted(BUILTIN_PROVIDERS | set(cfg.ai.providers))


# ---------------------------------------------------------------- reading the effective configuration

def _routes(db: Session, tenant_id: str) -> dict[str, AiRoute]:
    if tenant_id in _preset:
        return _preset[tenant_id][0]
    return {r.tier: r for r in db.execute(select(AiRoute).where(AiRoute.tenant_id == tenant_id)).scalars()}


def effective(db: Session, cfg: TenantConfig) -> TenantConfig:
    """The tenant config with the owners' panel choices applied (unknown providers in old rows are ignored)."""
    routes = _routes(db, cfg.id)
    if not routes:
        return cfg
    update, prices = {}, dict(cfg.ai.prices)
    for tier, r in routes.items():
        if r.provider not in providers(cfg):
            continue
        base = getattr(cfg.ai, tier)
        update[tier] = ModelRoute(provider=r.provider, model=r.model, effort=r.effort, max_tokens=base.max_tokens)
        if r.price_input is not None and r.price_output is not None:
            prices[r.model] = Price(input=r.price_input, output=r.price_output)
    return cfg.model_copy(update={"ai": cfg.ai.model_copy(update={**update, "prices": prices})})


def keys(db: Session, tenant_id: str) -> dict[str, str]:
    """Decrypted panel keys by provider. A key that cannot be decrypted (secret changed) is treated as missing."""
    if tenant_id in _preset:
        return _preset[tenant_id][1]
    f = _fernet()
    if f is None:
        return {}
    out = {}
    for k in db.execute(select(ProviderKey).where(ProviderKey.tenant_id == tenant_id)).scalars():
        try:
            out[k.provider] = f.decrypt(k.secret.encode()).decode()
        except InvalidToken:
            continue
    return out


def capture(db: Session, cfg: TenantConfig) -> tuple[dict, dict]:
    """This tenant's model choices and keys, detached from the database session."""
    return {t: _Detached(r) for t, r in _routes(db, cfg.id).items()}, keys(db, cfg.id)


@contextmanager
def preset(tenant_id: str, snapshot: tuple[dict, dict]):
    """Carry captured choices and keys into work that runs in another database (the quality suite)."""
    _preset[tenant_id] = snapshot
    try:
        yield
    finally:
        _preset.pop(tenant_id, None)


class _Detached:
    def __init__(self, r: AiRoute):
        self.provider, self.model, self.effort, self.price_input, self.price_output = r.provider, r.model, r.effort, r.price_input, r.price_output


# ---------------------------------------------------------------- changes by owners (audited)

def _owner(user: User):
    if user.role != "owner":
        raise SettingsError("only owners can change AI models and keys", 403)


def set_route(db: Session, cfg: TenantConfig, user: User, tier: str, provider: str, model: str, effort: str,
              price_input: float | None = None, price_output: float | None = None) -> AiRoute:
    _owner(user)
    model = model.strip()
    if tier not in TIERS:
        raise SettingsError("choose the careful (strong) or quick (cheap) AI")
    if provider not in providers(cfg):
        raise SettingsError(f"unknown provider {provider}")
    if not MODEL_ID.match(model):
        raise SettingsError("type the model name exactly as your provider lists it, e.g. claude-sonnet-5-5")
    if effort not in EFFORTS:
        raise SettingsError("unknown effort level")
    if (price_input is None) != (price_output is None) or any(p is not None and p < 0 for p in (price_input, price_output)):
        raise SettingsError("give both prices (input and output, per million tokens) or neither")
    row = db.get(AiRoute, (cfg.id, tier)) or AiRoute(tenant_id=cfg.id, tier=tier)
    db.add(row)
    row.provider, row.model, row.effort, row.price_input, row.price_output = provider, model, effort, price_input, price_output
    row.changed_by, row.changed_at = user.id, utcnow()
    label = "careful AI" if tier == "strong" else "quick AI"
    events.emit(db, cfg.id, "AI_SETTINGS_CHANGED", f"{user.name} set the {label} to {provider}:{model} ({effort})",
                actor=f"user:{user.id}", tier=tier, provider=provider, model=model, by=user.name)
    return row


def reset_route(db: Session, cfg: TenantConfig, user: User, tier: str) -> None:
    _owner(user)
    row = db.get(AiRoute, (cfg.id, tier))
    if row is not None:
        db.delete(row)
        default = getattr(cfg.ai, tier)
        events.emit(db, cfg.id, "AI_SETTINGS_CHANGED", f"{user.name} returned the {'careful' if tier == 'strong' else 'quick'} AI to the "
                    f"company default {default.provider}:{default.model}", actor=f"user:{user.id}", tier=tier, by=user.name)


def set_key(db: Session, cfg: TenantConfig, user: User, provider: str, key: str) -> ProviderKey:
    _owner(user)
    f = _fernet()
    if f is None:
        raise SettingsError("keys cannot be saved from the panel until your technical team sets WORKFORCE_SECRET_KEY", 409)
    if provider not in providers(cfg):
        raise SettingsError(f"unknown provider {provider}")
    key = key.strip()
    if len(key) < 8 or any(c.isspace() for c in key):
        raise SettingsError("that does not look like an API key")
    row = db.get(ProviderKey, (cfg.id, provider)) or ProviderKey(tenant_id=cfg.id, provider=provider)
    db.add(row)
    row.secret, row.hint, row.set_by, row.set_at = f.encrypt(key.encode()).decode(), key[-4:], user.id, utcnow()
    events.emit(db, cfg.id, "AI_KEY_SET", f"{user.name} saved an API key for {PROVIDER_LABELS.get(provider, provider)} (ending {row.hint})",
                actor=f"user:{user.id}", provider=provider, by=user.name)
    return row


def remove_key(db: Session, cfg: TenantConfig, user: User, provider: str) -> None:
    _owner(user)
    row = db.get(ProviderKey, (cfg.id, provider))
    if row is not None:
        db.delete(row)
        events.emit(db, cfg.id, "AI_KEY_REMOVED", f"{user.name} removed the API key for {PROVIDER_LABELS.get(provider, provider)}",
                    actor=f"user:{user.id}", provider=provider, by=user.name)


def overview(db: Session, cfg: TenantConfig) -> dict:
    """What the panel shows. Never contains a key."""
    import os

    from .providers import BUILTIN
    eff = effective(db, cfg).ai
    routes = _routes(db, cfg.id)
    names = {u.id: u.name for u in db.execute(select(User).where(User.tenant_id == cfg.id)).scalars()}
    saved = {k.provider: k for k in db.execute(select(ProviderKey).where(ProviderKey.tenant_id == cfg.id)).scalars()}
    usable = keys(db, cfg.id)
    tiers = {}
    for tier in TIERS:
        r, d, row = getattr(eff, tier), getattr(cfg.ai, tier), routes.get(tier)
        price = eff.prices.get(r.model)
        tiers[tier] = {"provider": r.provider, "model": r.model, "effort": r.effort,
                       "default": {"provider": d.provider, "model": d.model, "effort": d.effort},
                       "changed_in_panel": bool(row), "changed_by": names.get(getattr(row, "changed_by", None)),
                       "changed_at": row.changed_at.isoformat() + "Z" if row and getattr(row, "changed_at", None) else None,
                       "price": {"input": price.input, "output": price.output} if price else None}
    out = []
    for p in providers(cfg):
        conf = cfg.ai.providers.get(p) or BUILTIN[p]
        k = saved.get(p)
        env = bool(conf.api_key_env and os.environ.get(conf.api_key_env))
        source = "panel" if p in usable else ("environment" if env else ("not_needed" if not conf.api_key_env else "none"))
        out.append({"id": p, "label": PROVIDER_LABELS.get(p, p), "key": source, "hint": k.hint if k and p in usable else None,
                    "set_by": names.get(k.set_by) if k else None, "set_at": k.set_at.isoformat() + "Z" if k else None,
                    "unreadable": bool(k) and p not in usable})
    return {"tiers": tiers, "providers": out, "can_store_keys": can_store_keys(),
            "routing": {t: eff.tier_for(t) for t in ("investigation", "question", "brief", "notification")}}
