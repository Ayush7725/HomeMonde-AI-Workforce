"""Tenant packs: everything company-specific (sources, datasets, metrics, rules, AI employees, tools,
actions, approval authority and the world layout) is declared here and validated at startup.
Onboarding a new company means writing a pack, not changing code."""
import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import expr
from .settings import get_settings

ID = r"^[a-z][a-z0-9_]{0,62}$"
_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def resolve_env(value: str) -> str:
    """Expand ${VAR} and ${VAR:-default} references. Secrets never live in the pack itself."""
    def sub(m):
        if m.group(1) in os.environ:
            return os.environ[m.group(1)]
        if m.group(2) is not None:
            return m.group(2)
        raise ValueError(f"environment variable {m.group(1)} is not set")
    return _ENV.sub(sub, value)


Effort = Literal["low", "medium", "high", "xhigh", "max"]
Priority = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
# Providers built into the platform. Any other OpenAI-compatible service is declared under ai.providers.
BUILTIN_PROVIDERS = {"anthropic", "openai", "gemini", "deepseek", "huggingface"}
TASKS = ("investigation", "question", "brief", "notification")


class ProviderConfig(Strict):
    """A custom or overridden model provider. Keys are read from the named environment variable at call time."""
    kind: Literal["anthropic", "openai_compatible"] = "openai_compatible"
    base_url: str | None = None  # may reference ${ENV}
    api_key_env: str | None = Field(None, pattern=r"^[A-Z][A-Z0-9_]*$")
    structured: Literal["json_schema", "json_object", "prompt"] = "json_object"  # how the final JSON is enforced
    max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_effort: bool = False  # send the route's effort as reasoning_effort (reasoning models only)
    timeout_seconds: float = Field(120, ge=5, le=600)

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible providers need base_url")
        return self


class ModelRoute(Strict):
    """Which provider and model does a tier of work. Accepts a mapping or "provider:model[:effort]",
    which may come from the environment, e.g. "${HOMEMONDE_CHEAP_MODEL:-anthropic:claude-haiku-4-5}"."""
    provider: str = "anthropic"
    model: str
    effort: Effort = "medium"
    max_tokens: int = Field(8000, ge=500, le=64000)

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, v):
        if isinstance(v, str):
            parts = resolve_env(v).split(":")
            if len(parts) not in (2, 3) or not all(parts):
                raise ValueError(f"model route '{v}' must be provider:model or provider:model:effort")
            return {"provider": parts[0], "model": parts[1], **({"effort": parts[2]} if len(parts) == 3 else {})}
        return v


class Price(Strict):
    """USD per million tokens. Cache prices default to 10% (read) and 125% (write) of input."""
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(None, ge=0)
    cache_write: float | None = Field(None, ge=0)


class AiConfig(Strict):
    enabled: bool = True
    # strong: investigations, labelled statements with evidence refs, recommendations.
    # cheap: rewording already-verified text (briefs, notifications) and simple lookup questions.
    strong: ModelRoute = ModelRoute(provider="anthropic", model="claude-opus-5-5", effort="medium")
    cheap: ModelRoute = ModelRoute(provider="anthropic", model="claude-haiku-5-5", effort="low", max_tokens=4000)
    routing: dict[Literal["investigation", "question", "brief", "notification"], Literal["strong", "cheap"]] = {}
    # Cost controls. The defaults keep every alert on the strong model with a full AI explanation.
    # premium: an optional more capable model that replaces strong for alerts at the priorities in premium_for.
    premium: ModelRoute | None = None
    premium_for: list[Priority] = ["CRITICAL"]
    # Rule alerts at these priorities are explained by the AI straight away. The others get their verified figures
    # (no AI cost) and an "Explain" button that runs the AI explanation on request.
    auto_explain: list[Priority] = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    # A repeat of the same alert within this many days, whose figures moved by less than reuse_tolerance (relative),
    # reuses the earlier explanation instead of paying for a new one. 0 switches it off.
    reuse_unchanged_days: int = Field(0, ge=0, le=90)
    reuse_tolerance: float = Field(0.05, ge=0, le=1)
    providers: dict[str, ProviderConfig] = {}
    prices: dict[str, Price] = {}  # by model id; Claude prices are built in
    max_steps: int = Field(6, ge=1, le=20)
    max_investigation_seconds: int = Field(300, ge=30, le=3600)  # then the AI must conclude with what it has
    max_investigation_cost_usd: float | None = Field(None, gt=0)  # stop spending on one task (needs prices for the models)
    daily_token_budget: int = Field(2_000_000, ge=0)
    daily_cost_budget_usd: float | None = Field(None, ge=0)
    refusal_fallback: bool = True

    @model_validator(mode="after")
    def _check(self):
        for tier in ("strong", "cheap", "premium"):
            if getattr(self, tier) is None:
                continue
            p = getattr(self, tier).provider
            if p not in BUILTIN_PROVIDERS and p not in self.providers:
                raise ValueError(f"ai.{tier}: unknown provider '{p}' (built in: {sorted(BUILTIN_PROVIDERS)}, or declare it under ai.providers)")
        if self.routing.get("investigation") == "cheap":
            raise ValueError("investigations always run on the strong model")
        return self

    def tier_for(self, task: str) -> str:
        default = {"investigation": "strong", "question": "cheap", "brief": "cheap", "notification": "cheap"}[task]
        return self.routing.get(task, default)

    def investigation_tier(self, priority: str) -> str:
        return "premium" if self.premium is not None and priority in self.premium_for else "strong"


class Schedule(Strict):
    """A regular review an AI employee does on its own (Lock 12), e.g. a weekly supplier review."""
    id: str = Field(pattern=ID)
    employee: str
    every: Literal["daily", "weekly", "monthly"]
    title: str
    brief: str  # what to look at, in plain words; the employee investigates it with its own tools
    hour: int = Field(8, ge=0, le=23)  # local time
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "mon"  # weekly only
    day: int = Field(1, ge=1, le=28)  # monthly only
    priority: Priority = "LOW"


class Kpi(Strict):
    """A headline figure on the Home screen. Shown to owners and to managers of its area."""
    metric: str
    compare: str | None = None  # the same figure for the previous period
    label: str | None = None
    area: str
    good: Literal["up", "down"] = "up"


class HomeConfig(Strict):
    kpis: list[Kpi] = []


class ReportsConfig(Strict):
    enabled: bool = True
    daily_hour: int = Field(7, ge=0, le=23)  # local time the daily brief is prepared
    weekly_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "mon"
    monthly_day: int = Field(1, ge=1, le=28)


class NotifyConfig(Strict):
    """Optional delivery of material notifications to another system (chat, email gateway). Signed with HMAC."""
    webhook: str | None = None
    secret: str | None = None


class FieldSpec(Strict):
    type: Literal["string", "integer", "number", "date", "boolean"] = "string"
    column: str | None = None  # source column name; defaults to the field name
    required: bool = False
    sensitive: bool = False  # never sent to the LLM
    format: str | None = None  # date format when not ISO, e.g. "%d/%m/%Y" or "%m/%d/%Y"
    default: str | int | float | bool | None = None  # used when the cell is blank, e.g. reserved: 0


class SourceConfig(Strict):
    type: Literal["csv", "sql"]
    location: str  # directory for csv, SQLAlchemy URL for sql; may reference ${ENV}
    schedule_minutes: int = Field(30, ge=1)
    label: str = ""
    delimiter: str = Field(",", min_length=1, max_length=1)  # csv only
    owner: str = ""  # the person or team who fixes this feed when it breaks, shown on its data-problem tasks


class SignRule(Strict):
    """The sign convention of an amount by category, e.g. ledger revenue positive and every cost negative. Rows that break
    it are kept but reported, because a cost booked as positive silently inflates profit."""
    field: str
    by: str
    positive: list[str] = []
    negative: list[str] = []


class DatasetConfig(Strict):
    label: str
    source: str
    extract: str  # csv file name or SQL SELECT
    mode: Literal["upsert", "snapshot"] = "upsert"
    key: list[str]
    date_field: str | None = None
    max_age_minutes: int = Field(120, ge=1)
    max_reject_ratio: float = Field(0.05, ge=0, le=1)
    # Snapshot datasets keep one copy per day; older days than this are removed so the database stays bounded.
    keep_snapshot_days: int = Field(90, ge=1, le=3660)
    # The newest dated row may be at most this many days old; older means the export stopped moving even though it
    # is still being read, so the dataset is treated as stale. Needs date_field.
    max_data_age_days: int | None = Field(None, ge=0)
    signs: SignRule | None = None
    fields: dict[str, FieldSpec]

    @model_validator(mode="after")
    def _check(self):
        missing = [k for k in self.key + ([self.date_field] if self.date_field else []) if k not in self.fields]
        if self.signs:
            missing += [f for f in (self.signs.field, self.signs.by) if f not in self.fields]
        if missing:
            raise ValueError(f"key/date fields not declared: {missing}")
        if self.mode == "snapshot" and self.date_field:
            raise ValueError("snapshot datasets are dated by sync date; remove date_field")
        if self.max_data_age_days is not None and not self.date_field:
            raise ValueError("max_data_age_days needs a date_field")
        return self


class MetricConfig(Strict):
    label: str
    unit: str = ""
    version: str = "1.0"
    description: str = ""
    # aggregate metric
    dataset: str | None = None
    agg: Literal["sum", "count", "avg", "min", "max", "count_distinct"] | None = None
    value: str | None = None  # field name or arithmetic over row fields
    where: dict[str, str | int | float | bool | list] = {}
    window_days: int | None = Field(None, ge=1)
    window_offset_days: int = Field(0, ge=0)  # shift the window back, e.g. 30 = the 30 days before the last 30
    per_day: bool = False
    # derived metric
    formula: str | None = None
    by: list[str] = []

    @model_validator(mode="after")
    def _check(self):
        if bool(self.formula) == bool(self.dataset):
            raise ValueError("define either formula or dataset+agg")
        if self.dataset and not self.agg:
            raise ValueError("aggregate metrics need agg")
        if self.agg and self.agg not in ("count",) and not self.value:
            raise ValueError("aggregate metrics need value")
        if self.formula:
            expr.parse(self.formula)
        if self.value:
            expr.parse(self.value)
        return self


class ConflictCheck(Strict):
    dataset: str
    other: str
    match_on: str
    field: str
    other_field: str | None = None
    tolerance: float = 0


class InvoiceMatch(Strict):
    """Three-way match: every open supplier invoice against its purchase order and the goods received on it.
    All comparisons are deterministic; tolerances are shares (0.02 = 2%) confirmed with the accounts team."""
    invoices: str
    orders: str
    receipts: str
    invoice_field: str = "invoice_number"
    po_field: str = "po_number"
    supplier_field: str = "supplier"
    quantity_field: str = "quantity"  # on invoices, orders and receipts
    price_field: str = "unit_price"  # on invoices and orders
    status_field: str = "status"  # on invoices
    open_statuses: list[str] = ["OPEN", "ON_HOLD"]
    quantity_tolerance: float = Field(0, ge=0, le=1)
    price_tolerance: float = Field(0.01, ge=0, le=1)
    unit_field: str = "unit"  # on orders, receipts and invoices when the exports carry it; quantities compare only in one unit
    order_status_field: str = "status"
    cancelled_statuses: list[str] = ["CANCELLED"]  # an order with this status should not be billed; such invoices are not counted


class DuplicateCheck(Strict):
    """Duplicate and near-duplicate invoices: the same supplier invoice number entered twice, or the same supplier
    billing the same amount again within window_days under a different number."""
    dataset: str
    supplier_field: str = "supplier"
    number_field: str = "invoice_number"
    amount_field: str = "total_amount"
    date_field: str = "invoice_date"
    status_field: str = "status"
    open_statuses: list[str] = ["OPEN", "ON_HOLD"]  # at least one of the pair must still be unpaid
    window_days: int = Field(45, ge=1, le=366)
    amount_tolerance: float = Field(0.5, ge=0)  # in currency units


class OutcomeCheck(Strict):
    """Checks a rule's prediction later. correct_when sees the rule's metrics for the same group at check time,
    plus action_taken (1 if an approved action ran). Thresholds are per company and confirmed in Phase 0."""
    after_days: int = Field(ge=1, le=90)
    correct_when: str

    @model_validator(mode="after")
    def _check(self):
        expr.parse(self.correct_when)
        return self


class RuleConfig(Strict):
    id: str = Field(pattern=ID)
    version: str = "1.0"
    kind: Literal["threshold", "conflict", "invoice_match", "duplicate"] = "threshold"
    sentiment: Literal["problem", "opportunity"] = "problem"  # opportunities are good news worth acting on
    description: str = ""
    for_each: str | None = None
    when: str | None = None
    metrics: list[str] = []  # evidence pack computed before any AI runs
    conflict: ConflictCheck | None = None
    invoice_match: InvoiceMatch | None = None
    duplicate: DuplicateCheck | None = None
    work_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,40}$")
    employee: str
    priority: Priority = "MEDIUM"
    title: str
    subject: str = ""
    cooldown_hours: float = Field(24, ge=0)
    outcome_check: OutcomeCheck | None = None
    # Investigate every group that matches in one pass together (e.g. all SKUs at risk today) as a single task:
    # one AI investigation instead of one per group. Each group keeps its own cooldown.
    batch: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.batch and (self.kind != "threshold" or not self.for_each):
            raise ValueError(f"rule {self.id}: batch needs a threshold rule with for_each")
        if self.kind == "threshold":
            if not self.when:
                raise ValueError(f"rule {self.id}: threshold rules need when")
            expr.parse(self.when)
        elif not getattr(self, self.kind):
            raise ValueError(f"rule {self.id}: {self.kind} rules need {self.kind}")
        return self


class ParamSpec(Strict):
    type: Literal["string", "integer", "number"] = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    # Fixed by the evidence (e.g. the supplier's price): an approver may not change it. Without this, a manager could
    # lower a price to bring the amount under their approval limit.
    locked: bool = False


class ToolConfig(Strict):
    kind: Literal["lookup", "metrics"]
    description: str
    label: str = ""  # what people see, e.g. "Stock in the warehouse"; the description is for the AI
    area: str  # where the world draws the call
    spot: str | None = None  # optional named spot inside the area (e.g. a rack)
    params: dict[str, ParamSpec] = {}
    dataset: str | None = None
    fields: list[str] = []
    order_by: str | None = None
    limit: int = Field(20, ge=1, le=200)
    metrics: list[str] = []
    breakdown: str | None = None  # extra dimension for metric tools
    order: Literal["asc", "desc"] = "asc"  # metric tools: row order when several groups match

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "lookup" and (not self.dataset or not self.fields):
            raise ValueError("lookup tools need dataset and fields")
        if self.kind == "metrics" and not self.metrics:
            raise ValueError("metric tools need metrics")
        return self


class Approver(Strict):
    role: Literal["owner", "manager"]
    max_amount: float | None = None


class EmailConfig(Strict):
    """How an approved message is sent. smtp_url is smtp://user:password@host:587 (STARTTLS) or smtps://...:465,
    normally from the environment, e.g. ${HOMEMONDE_SMTP_URL}. The message text and recipient come from the action's
    params; nothing is sent before a person approves it."""
    smtp_url: str
    sender: str
    reply_to: str | None = None
    to_param: str = "to"
    subject_param: str = "subject"
    body_param: str = "message"


class ActionConfig(Strict):
    label: str
    description: str
    handler: Literal["record", "webhook", "email"] = "record"
    url: str | None = None
    secret: str | None = None
    email: EmailConfig | None = None
    params: dict[str, ParamSpec]
    amount: str | None = None  # expression over params, e.g. "quantity * unit_price"
    level: Literal[1, 2, 3] = 2  # 1 auto, 2 human approves, 3 human only
    area: str
    approvers: list[Approver] = [Approver(role="owner")]

    @model_validator(mode="after")
    def _check(self):
        if self.handler == "webhook" and not self.url:
            raise ValueError("webhook actions need url")
        if self.handler == "email":
            if not self.email:
                raise ValueError("email actions need email settings")
            missing = {self.email.to_param, self.email.subject_param, self.email.body_param} - set(self.params)
            if missing:
                raise ValueError(f"email actions need params {sorted(missing)}")
        if self.amount:
            unknown = expr.names(self.amount) - set(self.params)
            if unknown:
                raise ValueError(f"amount uses unknown params {unknown}")
        return self


class EmployeeConfig(Strict):
    name: str
    title: str
    role: str  # one-paragraph responsibility, used in the AI's instructions
    prefix: str = Field(pattern=r"^[A-Z]{2,5}$")
    area: str
    color: str = Field("#4A79F0", pattern=r"^#[0-9A-Fa-f]{6}$")
    tools: list[str] = []
    can_ask: list[str] = []
    actions: list[str] = []
    answers_questions: bool = False
    sensitive: bool = False  # works with personal data: only owners and managers of its area see its work
    effort: Effort | None = None
    max_steps: int | None = Field(None, ge=1, le=20)
    test_runs: int | None = Field(None, ge=1, le=5)  # times each quality scenario is repeated; default 3 if it can act, else 1


class AreaLayout(Strict):
    name: str
    subtitle: str = ""
    building: Literal["warehouse", "factory", "tower", "store", "office", "pavilion"]
    footprint: tuple[float, float, float, float]  # x, y, width, depth in tiles
    height: float = 2.0
    door: tuple[float, float]
    sign: str = ""
    spots: dict[str, tuple[float, float]] = {}


class Prop(Strict):
    type: Literal["tree", "bush", "lamp", "staff", "pallets", "rack", "tanks", "crates", "truck"]
    at: tuple[float, float]
    size: float = 1.0
    label: str = ""
    area: str | None = None  # clicking it opens this place
    dataset: str | None = None  # trucks: drive out when new rows arrive in this dataset (e.g. shipments)


class LayoutConfig(Strict):
    extent: tuple[float, float] = (24, 16)
    areas: dict[str, AreaLayout]
    roads: list[tuple[float, float, float, float]] = []
    walkways: list[tuple[float, float, float, float]] = []
    paths: list[list[tuple[float, float]]]
    props: list[Prop] = []

    @model_validator(mode="after")
    def _check(self):
        if "approvals" not in self.areas:
            raise ValueError("layout needs an 'approvals' area (where decisions wait for people)")
        return self


class TenantConfig(Strict):
    id: str = Field(pattern=ID)
    name: str
    business: str  # what the company does; defines the AI's business scope boundary
    timezone: str = "UTC"
    currency: str = "USD"
    # How numeric dates like 05/10/2026 are written in this company's exports. Unset: only unambiguous ones are read.
    date_order: Literal["DMY", "MDY"] | None = None
    ai: AiConfig = AiConfig()
    sources: dict[str, SourceConfig]
    datasets: dict[str, DatasetConfig]
    metrics: dict[str, MetricConfig] = {}
    rules: list[RuleConfig] = []
    employees: dict[str, EmployeeConfig]
    tools: dict[str, ToolConfig] = {}
    actions: dict[str, ActionConfig] = {}
    schedules: list[Schedule] = []
    reports: ReportsConfig = ReportsConfig()
    home: HomeConfig = HomeConfig()
    notify: NotifyConfig = NotifyConfig()
    layout: LayoutConfig

    @model_validator(mode="after")
    def _cross_refs(self):
        errs = []
        areas = set(self.layout.areas)
        for k in [*self.datasets, *self.metrics, *self.employees, *self.tools, *self.actions, *self.sources, *areas]:
            if not re.match(ID, k):
                errs.append(f"invalid id '{k}' (lowercase letters, digits, underscore)")
        for name, d in self.datasets.items():
            if d.source not in self.sources:
                errs.append(f"dataset {name}: unknown source {d.source}")
        for name, m in self.metrics.items():
            if m.dataset:
                ds = self.datasets.get(m.dataset)
                if not ds:
                    errs.append(f"metric {name}: unknown dataset {m.dataset}")
                    continue
                used = set(m.by) | set(m.where) | (expr.names(m.value) if m.value else set())
                derived = {f"days_until_{f}" for f, spec in ds.fields.items() if spec.type == "date"}
                bad = used - set(ds.fields) - derived
                if bad:
                    errs.append(f"metric {name}: fields {sorted(bad)} not in dataset {m.dataset}")
                if m.window_days and not ds.date_field:
                    errs.append(f"metric {name}: window_days needs a dated dataset")
                if m.window_offset_days and not m.window_days:
                    errs.append(f"metric {name}: window_offset_days needs window_days")
            else:
                bad = expr.names(m.formula) - set(self.metrics)
                if bad:
                    errs.append(f"metric {name}: formula uses unknown metrics {sorted(bad)}")
        for r in self.rules:
            if r.employee not in self.employees:
                errs.append(f"rule {r.id}: unknown employee {r.employee}")
            if r.kind == "threshold":
                bad = (expr.names(r.when) | set(r.metrics)) - set(self.metrics)
                if bad:
                    errs.append(f"rule {r.id}: unknown metrics {sorted(bad)}")
                for mname in expr.names(r.when) | set(r.metrics):
                    if mname in self.metrics and r.for_each and r.for_each not in self.metrics[mname].by:
                        errs.append(f"rule {r.id}: metric {mname} is not grouped by {r.for_each}")
                if r.outcome_check:
                    bad = expr.names(r.outcome_check.correct_when) - set(self.metrics) - {"action_taken"}
                    if bad:
                        errs.append(f"rule {r.id}: outcome_check uses unknown names {sorted(bad)}")
            elif r.conflict:
                for ds, f in ((r.conflict.dataset, r.conflict.field), (r.conflict.other, r.conflict.other_field or r.conflict.field)):
                    if ds not in self.datasets or f not in self.datasets[ds].fields or r.conflict.match_on not in self.datasets[ds].fields:
                        errs.append(f"rule {r.id}: conflict fields missing in {ds}")
            elif r.invoice_match:
                m = r.invoice_match
                need = {m.invoices: {m.invoice_field, m.po_field, m.supplier_field, m.quantity_field, m.price_field, m.status_field},
                        m.orders: {m.po_field, m.quantity_field, m.price_field}, m.receipts: {m.po_field, m.quantity_field}}
                for ds, fields in need.items():
                    if ds not in self.datasets or fields - set(self.datasets[ds].fields):
                        errs.append(f"rule {r.id}: invoice_match fields missing in {ds}")
            elif r.duplicate:
                d = r.duplicate
                fields = {d.supplier_field, d.number_field, d.amount_field, d.date_field, d.status_field}
                if d.dataset not in self.datasets or fields - set(self.datasets[d.dataset].fields):
                    errs.append(f"rule {r.id}: duplicate fields missing in {d.dataset}")
        for name, t in self.tools.items():
            if t.area not in areas:
                errs.append(f"tool {name}: unknown area {t.area}")
            elif t.spot and t.spot not in self.layout.areas[t.area].spots:
                errs.append(f"tool {name}: unknown spot {t.spot} in area {t.area}")
            if t.dataset and t.dataset not in self.datasets:
                errs.append(f"tool {name}: unknown dataset {t.dataset}")
            elif t.dataset:
                ds = self.datasets[t.dataset]
                bad = (set(t.fields) | set(t.params) | ({t.order_by} if t.order_by else set())) - set(ds.fields)
                if bad:
                    errs.append(f"tool {name}: fields {sorted(bad)} not in {t.dataset}")
                sens = [f for f in t.fields if f in ds.fields and ds.fields[f].sensitive]
                if sens:
                    errs.append(f"tool {name}: exposes sensitive fields {sens} to the AI")
            for m in t.metrics:
                if m not in self.metrics:
                    errs.append(f"tool {name}: unknown metric {m}")
                elif not set(t.params) <= set(self.metrics[m].by):
                    errs.append(f"tool {name}: params must be dimensions of metric {m}")
        for name, a in self.actions.items():
            if a.area not in areas:
                errs.append(f"action {name}: unknown area {a.area}")
        prefixes = set()
        for name, e in self.employees.items():
            if e.area not in areas:
                errs.append(f"employee {name}: unknown area {e.area}")
            if e.prefix in prefixes:
                errs.append(f"employee {name}: prefix {e.prefix} already used")
            prefixes.add(e.prefix)
            for t in e.tools:
                if t not in self.tools:
                    errs.append(f"employee {name}: unknown tool {t}")
                elif e.sensitive and self.tools[t].kind == "lookup":
                    errs.append(f"employee {name}: sensitive employees may only use aggregate (metrics) tools, not {t}")
            for c in e.can_ask:
                if c not in self.employees or c == name:
                    errs.append(f"employee {name}: cannot ask {c}")
            for a in e.actions:
                if a not in self.actions:
                    errs.append(f"employee {name}: unknown action {a}")
        for p in self.layout.props:
            if p.area and p.area not in areas:
                errs.append(f"layout prop at {p.at}: unknown area {p.area}")
            if p.dataset and (p.type != "truck" or p.dataset not in self.datasets):
                errs.append(f"layout prop at {p.at}: dataset only applies to trucks and must exist")
        seen = set()
        for sc in self.schedules:
            if sc.employee not in self.employees:
                errs.append(f"schedule {sc.id}: unknown employee {sc.employee}")
            if sc.id in seen:
                errs.append(f"schedule {sc.id}: duplicate id")
            seen.add(sc.id)
        for k in self.home.kpis:
            for m in filter(None, (k.metric, k.compare)):
                if m not in self.metrics:
                    errs.append(f"home kpi: unknown metric {m}")
                elif self.metrics[m].by:
                    errs.append(f"home kpi: {m} must be a single company-wide figure (no by)")
            if k.area not in areas:
                errs.append(f"home kpi {k.metric}: unknown area {k.area}")
        if not any(e.answers_questions for e in self.employees.values()):
            errs.append("one employee must have answers_questions: true (management questions)")
        if errs:
            raise ValueError("; ".join(errs))
        return self

    def question_employee(self) -> str:
        return next(k for k, e in self.employees.items() if e.answers_questions)

    def effort_for(self, emp: str) -> str:
        return self.employees[emp].effort or self.ai.strong.effort

    def steps_for(self, emp: str) -> int:
        return self.employees[emp].max_steps or self.ai.max_steps


def pack_path(slug: str) -> Path:
    return get_settings().tenants_dir / slug / "tenant.yaml"


def load_file(path: Path) -> tuple[TenantConfig, str]:
    raw = path.read_bytes()
    data = yaml.safe_load(raw)
    cfg = TenantConfig.model_validate(data)
    if cfg.id != path.parent.name:
        raise ValueError(f"tenant id '{cfg.id}' must match its directory '{path.parent.name}'")
    return cfg, hashlib.sha256(raw).hexdigest()


@lru_cache
def get(slug: str) -> TenantConfig:
    return load_file(pack_path(slug))[0]


def register_all(db) -> dict[str, TenantConfig]:
    """Validate every pack this deployment serves and make sure its tenant row exists. Fails loudly on bad config."""
    from .models import Tenant
    out = {}
    for slug in available():
        cfg, digest = load_file(pack_path(slug))
        row = db.get(Tenant, slug)
        if row is None:
            db.add(Tenant(id=slug, name=cfg.name, config_hash=digest))
        else:
            row.name, row.config_hash = cfg.name, digest
        out[slug] = cfg
    db.flush()
    return out


def available() -> list[str]:
    s = get_settings()
    if s.tenant_slugs:
        return s.tenant_slugs
    root = s.tenants_dir
    return sorted(p.parent.name for p in root.glob("*/tenant.yaml") if not p.parent.name.startswith("_")) if root.exists() else []
