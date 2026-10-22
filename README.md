# AI Workforce

A multi-company platform for AI employees that watch a business's own data, investigate what changes,
and bring evidence-backed findings and recommendations to founders and authorised managers, who make
the decisions. HomeMonde is the first deployment; every company-specific detail lives in a tenant
pack, so a new company is onboarded with configuration rather than code changes.

## What it does for a manufacturer

One AI employee per department reads that department's exports and raises work only when a rule says
something needs attention. The HomeMonde pack covers:

| Area | What is watched | What the AI prepares for a person to approve |
|---|---|---|
| Stock | Products with fewer than 5 days of cover (all at-risk SKUs investigated together) | Purchase request for the missing material |
| Factory | Lines below 85% of plan; downtime causes | Batch reschedule (done by a person) |
| Purchasing | Open purchase orders past their promised date, or whose delivery date slipped; the production they hold up | Follow-up message to the supplier |
| Accounts payable | Three-way match of every open invoice against its order and goods received; duplicate and near-duplicate invoices | Credit-note or correction query to the supplier |
| Sales, ads, finance | Channel growth, ad returns falling or rising, profit falling against last month | Ad budget change |
| Logistics, people | Couriers delivering late; departments with high absence (totals only) | Carrier escalation |
| Data itself | A channel that stops sending orders; a blocked batch; files that are empty, stale, contradict themselves or have blank amounts | A data task for the person who owns the export |

Alerts close by themselves when a later check shows the problem has gone (stock arrived, order received);
a batched alert shows which of its items are already back to normal.

Numbers, matches and thresholds are computed by code; the AI explains causes across departments and
drafts the next step. Payments, orders and production plans are changed only by people.

## How it works

```
Source systems ─► Connectors ─► Validation ─► Trusted records (raw → normalised, with history)
                                                   │
                         Metric registry (deterministic) ─► Rules ─► Work queue
                                                                        │
            AI employee runtime ◄── controlled tools (least privilege, compact, cited) ──┘
                   │
         Claude (reasoning only) ─► Validation guard (grounding, freshness, permissions)
                   │
     Finding ─► Executive AI ─► Approval desk ─► Human decision ─► Pre-action checks ─► Action ─► Verify
                   │
          Hash-chained event log ─► Activity stream · Audit trail · Miniature world view
```

- **The AI is not the source of truth.** Numbers come from the metric engine, tools and the
  `calculate` tool; the model reads them and explains them.
- **Work is created by rules**, never by free-form model reasoning. Rules don't fire on stale data.
- **Every claim is grounded.** Facts and observations must cite evidence refs (`E1`, `C1`, `M1`); every
  number written in a claim must appear in the evidence it cites. The model gets one repair turn;
  anything it still can't ground is removed and listed as a validation note.
- **Statements are labelled** FACT, OBSERVATION, INFERENCE, HYPOTHESIS or RECOMMENDATION, with an
  evidence quality (CONFIRMED, LIKELY, POSSIBLE, INSUFFICIENT_EVIDENCE, DATA_CONFLICT, DATA_STALE).
- **People decide.** Actions run only after an authorised approver decides (role, area and amount
  limits), then pass pre-action checks, run once (idempotent), and are verified.
- **Failures are visible.** Source failures, rejected batches, tool errors, retries and failed actions
  are all events. Without AI credentials or budget, employees still report verified facts.
- **The world view only reflects events.** It never creates work and can be paused or switched off.

## Repository layout

```
workforce/
  settings.py   platform settings (environment variables, WORKFORCE_ prefix)
  tenancy.py    tenant pack schema and validation
  data.py       connectors (csv, sql), validation, ingestion, freshness
  metrics.py    deterministic metric engine
  expr.py       safe expression evaluator (formulas, rules, calculate tool)
  rules.py      threshold, source-conflict, three-way-match and duplicate rules → work items
  work.py       durable work queue with leases and retries
  tools.py      controlled tools, colleague requests, evidence store
  providers.py  model provider adapters (Anthropic SDK; any OpenAI-compatible API)
  llm.py        model routing (cheap / strong / premium), budgets, usage and cost ledger
  wording.py    cheap-model rewording of verified text, with the number check
  reports.py    daily, weekly and monthly briefs
  notify.py     notifications for material events (in-app, optional signed webhook)
  health.py     pause/kill switch, health per AI employee, worker heartbeat
  quality.py    reviews, accuracy, outcome checks, rule reviews and the scenario test suite
  validation.py grounding and output guard
  runtime.py    AI employee investigation loop
  actions.py    approvals, authority checks, action execution
  events.py     hash-chained event log
  security.py   accounts, sessions, lockout
  api.py        HTTP API and web app
  worker.py     background worker
  cli.py        operator commands
  web/          management web app (world view, inspector, approvals)
tenants/<id>/tenant.yaml   one pack per company (HomeMonde is the first)
demo/                      demo companies, dummy-data generator, one-command runner and soak test
migrations/                database migrations (Alembic)
tests/                     end-to-end tests with a scripted stand-in for Claude
```

## Run it locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export WORKFORCE_SECURE_COOKIES=false HOMEMONDE_EXPORTS_DIR=$PWD/exports ANTHROPIC_API_KEY=...
workforce check                     # validate tenant packs
workforce migrate                   # create the SQLite database (workforce.db)
workforce user-add --tenant homemonde --email you@company.com --name "Your Name" --role owner
workforce serve --port 8000 &       # web app at http://localhost:8000
workforce worker                    # syncs, rules, AI work
pytest                              # tests never call the Claude API
```

Put the company's exports (CSV files named in the pack) in `HOMEMONDE_EXPORTS_DIR`.

## Try the demo (no real data needed)

With Docker (Postgres included, nothing else to install):

```bash
docker compose -f docker-compose.demo.yml up --build      # then open http://127.0.0.1:8100
```

Or locally:

```bash
pip install -e ".[dev]"
python demo/run.py            # add --reset to start from an empty database
```

AI is optional. To see the employees investigate, copy `.env.demo.example` to `.env.demo` and add a key
(a free Google AI Studio key works for a demo), or sign in as an owner and paste one in
Operations › AI models & keys.

This generates dummy data for four made-up companies, creates accounts, and starts the API and
worker at http://127.0.0.1:8100 (every demo account uses the password `demo-workforce-2026`).
The companies are deliberately different, so each part of the platform is exercised:

| Company | Business | Data shape | Situations planted in the data |
|---|---|---|---|
| HomeMonde | Home textiles maker, India, 10 AI employees | CSV exports from seven systems, ISO dates, INR | Stockout with blocked production; the late fabric order behind it; stitching absences hold back a line; Meta ads stop paying back; profit falls on fees and ad spend; one courier delivers late; a short delivery billed in full; an invoice entered twice |
| Kavya Foods | Packaged foods maker, India | SQL database (ERP tables and column names), batch expiry dates | Stock expiring before it can be sold; stockout with a production order on hold |
| Atlas Auto Parts | Parts distributor, USA | US dates, `$1,234.50`, `64%`, two warehouses, WMS + ERP | WMS and ERP disagree; a vendor with late POs; stockout; an automatic (level 1) transfer |
| Bloom Apparel | Online fashion, UK | Semicolon CSVs, UK dates, GBP, no factory | Return-rate spike; stockout; an ads feed that is missing on purpose |

Every company also has a few invalid rows (skipped and reported) and many healthy products that
must not trigger anything. `tests/test_demo_companies.py` checks that exactly the planted
situations are found. Sign in as two companies in two browsers to see that tenants never mix.
Set `ANTHROPIC_API_KEY` before starting to watch the AI employees investigate with Claude.
For a demo that behaves like production, run it on Postgres: `python demo/run.py --database-url postgresql+psycopg://...`.

**Before a demo or release, soak it.** `demo/soak.py` runs the API and two workers on the demo companies
for as long as you choose, signs in as every demo manager and uses the app (browsing, questions, decisions,
Explain), keeps a live stream open, and records memory, database size, response times and health every
minute. `--chaos` also kills a worker mid-run and restarts Postgres. It ends with a pass/fail verdict:

```bash
python demo/soak.py --database-url postgresql+psycopg://user:pw@127.0.0.1:5432/soak --hours 24 --chaos --pg-container <name>
```

## Deploy for a company

Each company gets its own deployment (its own server and database). One deployment can also serve
several tenants; every row is tenant-scoped and users only ever see their own tenant.

1. Copy `.env.example` to `.env` and fill it in: database password, allowed host, Claude API key,
   and any `${VAR}` the tenant pack references.
2. Put the tenant pack in `tenants/<id>/tenant.yaml` and run `workforce check`.
3. `docker compose up -d` (Postgres, migrations, API, worker and a daily backup into `./backups`).
   Logs are size-limited, the worker has a health check, and it restarts itself if its loop ever stalls.
4. Put the API behind a TLS reverse proxy. Cookies are `Secure`, `HttpOnly` and `SameSite=Strict`.
5. Create accounts: `docker compose exec api workforce user-add --tenant <id> ...`.
   Only owners and authorised managers get accounts; ordinary employees never access the system.

Scale by running more `worker` containers (Postgres `SKIP LOCKED` keeps them from colliding).

## Onboarding a company

Write `tenants/<id>/tenant.yaml`. The schema (`workforce/tenancy.py`) rejects unknown keys and
cross-checks every reference at startup.

| Section | What it declares |
|---|---|
| `business` | What the company does. This is the AI's scope boundary; anything outside it is declined. |
| Action params | `locked: true` keeps a value fixed by the evidence (e.g. a supplier's price) from being changed when approving. |
| Dataset `signs` | The sign convention by category (e.g. ledger revenue positive, costs negative). Rows that break it are kept and reported. |
| `currency` | The company's currency. Amounts written with another currency's symbol (`$` in an INR company) are refused, never counted as if they were rupees. |
| `date_order` | `DMY` or `MDY`: how the exports write numeric dates such as `05/10/2026`. Unset, only unambiguous ones (a month name, or a day above 12) are read; the rest are refused and reported. |
| `sources` | Where data comes from (`csv` folder or read-only `sql` database). Secrets as `${ENV}`. |
| `datasets` | Each export or query: key, date field, typed fields (with `column` mapping and a `format` for non-ISO dates), freshness limit, reject limit. Amounts like `$1,234.50`, `₹ 4,500` or `64%` are understood; anything else (`1e3`, `12 pcs`, `N/A`) is refused, never guessed. Text values match whatever their case (`Delivered` is `delivered`), and an event dated after today is refused. Mark personal data `sensitive: true`; it is never sent to the AI. |
| `metrics` | Versioned deterministic definitions: aggregates over a dataset or formulas over other metrics. Date fields can be used as `days_until_<field>` (for example expiry). |
| `rules` | When work is created (threshold or source conflict), which employee owns it, priority, cooldown. |
| `employees` | The AI workforce: responsibility, area, tools, colleagues they may ask, actions they may recommend. Channels and markets are contexts, not separate employees. |
| `tools` | What each tool may read: dataset and whitelisted fields, or metrics. Where the world draws it. |
| `actions` | What may be recommended: parameters, amount formula, level (1 auto, 2 approve, 3 human only), approvers and limits, and the handler (`record` register, signed `webhook`, or `email` through the company's mail server, sent only after approval). |
| `layout` | The miniature world: areas and building types, roads, walk paths, props. |

Before go-live, confirm with the company (Phase 0 discovery): the authoritative source for each
dataset, column mappings, metric definitions, rule thresholds, and the approval matrix. The HomeMonde
pack marks its current values as starting assumptions.

## Models, providers and cost

Each company sets two default model tiers in its pack, by config or environment variable. Owners can
switch either tier to another provider and model, and paste its key, from **Operations › AI models &
keys** without a restart; the pack stays the default they can return to:

```yaml
ai:
  strong: ${ACME_STRONG_MODEL:-anthropic:claude-sonnet-5-5:medium}   # provider:model[:effort]
  cheap: ${ACME_CHEAP_MODEL:-anthropic:claude-haiku-5-5:low}
  premium: ${ACME_PREMIUM_MODEL:-anthropic:claude-opus-5-5:medium}   # optional
  routing: {question: cheap, brief: cheap, notification: cheap}
  premium_for: [CRITICAL]          # alerts at these priorities use the premium model
  auto_explain: [CRITICAL, HIGH]   # other alerts report verified figures, with an Explain button
  reuse_unchanged_days: 7          # a repeat alert whose figures moved < reuse_tolerance reuses the explanation
  reuse_tolerance: 0.05
  providers:                       # any other OpenAI-compatible service
    inhouse: {base_url: "https://llm.internal/v1", api_key_env: INHOUSE_KEY, structured: json_schema}
  prices: {some-model-id: {input: 0.5, output: 1.5}}   # USD per million tokens, for cost comparison
  daily_token_budget: 2000000
  daily_cost_budget_usd: 5
```

**Keeping AI cost low.** Most alerts need no model at all to state the facts; the model is paid for the
explanation. Four controls decide when that happens, all per company: `premium_for` keeps the most capable
(and most expensive) model for the alerts that justify it; `auto_explain` explains only the priorities a
company cares about straight away; `reuse_unchanged_days` stops paying again for an alert whose figures have
not moved; and `batch: true` on a per-SKU rule investigates every SKU at risk in one pass together. The
daily cost budget is a hard ceiling; when it is reached, employees keep reporting verified figures. With the
HomeMonde settings a typical month costs roughly USD 15–40 in model usage (an estimate; the **AI usage &
quality** view shows the real figure).

Built-in providers: `anthropic` (official SDK), `openai`, `gemini`, `deepseek`, `huggingface`
(their OpenAI-compatible endpoints). Keys come from the panel (encrypted with `WORKFORCE_SECRET_KEY`)
or else from `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `HF_TOKEN`, or
the `api_key_env` of a custom provider.

- **Strong tier:** every investigation, every evidence-labelled statement and every recommendation
  (the premium tier instead, for priorities in `premium_for`).
- **Cheap tier:** briefs, notifications and summaries (rewording verified text only), and simple
  lookup questions. Questions start on the cheap tier without the right to recommend; if the answer
  fails the evidence check, or the model says it needs analysis, the strong tier redoes it.
- **Rewording check:** every line the cheap model writes must contain only numbers that appear,
  exactly, in its verified source. Failing lines are retried once on the strong tier; anything still
  failing keeps the verified wording.
- The same validation, budgets, refusal handling and usage records apply on every provider. Usage is
  recorded per call with provider, model, tier, task, tokens and cost (`llm_usage`). Claude prices are
  built in; other models show "price not set" until `ai.prices` has them.
- Compare providers on identical scenarios: `workforce eval --tenant homemonde --strong openai:<model>`.
  The **AI usage & quality** view shows pass rate, tokens and cost per model pair.

Only the Anthropic adapter and the request formats are covered by automated tests; check each other
provider with `workforce eval` before relying on it (structured-output support varies by provider).

## Operations and quality

- **Pause/kill switch** (owners): an employee's Health tab. A paused employee takes no new work, runs
  no actions (including automatic level 1 actions) and refuses colleague requests; work in progress
  returns to the queue at its next step. Every pause and resume is audited.
- **Health per employee:** HEALTHY, DEGRADED, FAILED, PAUSED or NEEDS_ATTENTION, with the reasons,
  queue, failures, share of findings where validation removed claims, accuracy, test pass rate and
  AI spend.
- **Accuracy:** people mark findings Correct / Partly / Wrong; rules can also check their own
  prediction later (`outcome_check`). A person's verdict overrides the automatic one.
- **Scenario suite:** `tenants/<id>/evals.yaml` lists known situations and expected results; each runs
  in a throwaway database through the real pipeline.
- **Briefs:** daily (at `reports.daily_hour` local time), weekly and monthly, from verified findings,
  decisions, data health and workforce health. Managers see only items from their areas.
- **Notifications:** only for high-priority findings, decisions waiting, failed actions and data
  sources that fail twice in a row. Optional delivery to a webhook (`notify.webhook`, HMAC-signed).
- **Sensitive employees** (`sensitive: true`, e.g. People/HR) may only use aggregate tools; only owners
  and managers of their area see their work.
- **Stopping and limits:** owners can stop a waiting or running task (it stops at its next step), pause
  one action type or one data feed, and every investigation has a time limit
  (`ai.max_investigation_seconds`) and an optional spending limit (`ai.max_investigation_cost_usd`).
- **Certainty from evidence:** the model's rating can only be lowered. Facts only → up to Confirmed;
  with the AI's interpretation → up to Likely if the facts come from two or more kinds of records,
  otherwise Possible; sources disagree → Data conflict; old data → Data stale. The reasons are shown
  with every finding. Suggested actions on weaker evidence are held back, and urgent items on weak
  evidence go to a person.
- **Overrides:** how often people changed, rejected or sent back an employee's suggestions (30 days).
  Above 40% over at least 5 decisions, the employee shows as Needs attention.
- **Rules learn from corrections:** when people turn down more than half of a rule's judged alerts (at
  least 5 in 30 days, by review verdict or by rejecting its suggestion), owners get a review task with the
  rule's track record. Rules are never changed automatically; an owner updates the pack.
- **Wrong answers become tests:** marking a finding Wrong (or a failed outcome check) asks an owner to
  add it to the tests. `workforce eval-draft --tenant <id> --work <ID>` (or the task's "Test draft"
  download) rebuilds the situation from the exact source records, with personal fields anonymised; the
  right answer is written by a person and the draft fails until they do.
- **Risk-based testing:** scenarios for employees that can propose actions run 3 times and every run
  must pass (`test_runs` per employee, or `runs` per scenario, overrides this).
- **Data problems are tasks:** a feed that fails twice in a row, or a file refused for too many bad
  rows, becomes one task for the AI employee whose area depends on it, naming who fixes it
  (`sources.<id>.owner`). It closes itself once the data comes through cleanly.
- **Temporary access** (owners, Team & access): let a manager see extra areas for 1–168 hours, with a
  reason. It ends on its own, can be ended early, and is in the audit log. It never widens what
  that manager may approve.
- **AI models & keys** (owners, Operations › AI models & keys): choose the AI company (Claude, ChatGPT,
  Gemini, DeepSeek, Hugging Face, or one declared under `ai.providers`), the model and how hard it thinks,
  separately for the careful and the quick AI, and test the connection. Keys pasted there are encrypted with
  `WORKFORCE_SECRET_KEY`, never shown again (only the last 4 characters) and never logged; a key set in the
  environment is used when none is saved. The tenant pack stays the company default. Every change is audited.
- **Regular reviews:** `schedules` in the tenant pack run daily, weekly or monthly reviews at a local
  hour, once per period.

## Phase 0 assumptions (HomeMonde)

Everything below is a starting assumption in `tenants/homemonde/` and must be confirmed against
HomeMonde's real systems before go-live:

- Export file names, column names and refresh frequency for inventory, orders, production plan,
  purchase orders, supplier scorecard, production log, ads, ledger, courier, HRMS, goods receipt (GRN)
  and supplier invoice exports; purchase-order and invoice status values; order unit prices.
- Ledger sign convention (revenue positive, costs negative) and account names.
- Rule thresholds: stock cover under 5 days, production under 85% of plan, ROAS under 70% of its 28-day
  level, profit 15% below the previous 30 days, more than 20% late deliveries, absence above 15%,
  delivery dates slipping 3 or more days, invoice price tolerance 1% and quantity tolerance 0, and the
  45-day window for near-duplicate invoices.
- How approved supplier follow-ups and invoice queries are sent (they are kept in the action register
  until a mail or ERP connection is chosen).
- Outcome-check definitions, approval limits per role and action, and who manages which area.
- The scenario suite in `evals.yaml` is synthetic; replace it with real past incidents.
- Who fixes each data feed (`owner` on each source) and the regular review schedule.

## Security

- scrypt password hashes, lockout after 5 failed sign-ins, hashed session tokens, CSRF tokens on
  every change, strict CSP and security headers, optional host allow-list.
- Managers see only the AI employees, work and approvals in their areas, and can approve only within
  their limits. Owners see everything in their tenant.
- AI employees read data only through their configured tools. Colleague requests run under the
  colleague's tools, and permissions are enforced in code, never in prompts.
- AI keys pasted in the panel are owner-only, encrypted at rest (Fernet, key derived from
  `WORKFORCE_SECRET_KEY`), never returned by the API, never logged and never in the event log.
- `workforce verify-audit --tenant <id>` (or `GET /api/audit/verify`) recomputes the event hash chain.

## Operations

| Command | Purpose |
|---|---|
| `workforce check` | Validate tenant packs |
| `workforce eval --tenant <id> [--strong p:m] [--cheap p:m]` | Run the AI-quality scenarios (real model calls when keys are set) |
| `workforce report --tenant <id> --kind daily` | Prepare a brief now (also scheduled automatically) |
| `workforce migrate` | Apply database migrations |
| `workforce serve` / `workforce worker` | Run the API / the worker |
| `workforce worker-health` | Exit non-zero when no worker has looped in 3 minutes (container health check) |
| `workforce sync --tenant <id>` | Sync now and run rules |
| `workforce user-add` / `user-disable` | Manage accounts (disable also ends sessions) |
| `workforce verify-audit --tenant <id>` | Check the audit chain |
| `workforce eval-draft --tenant <id> --work <ID>` | Turn a finding into a quality-test draft |

Health check: `GET /api/health` (database reachable, plus the worker's last heartbeat). Worker items
whose lease expires are re-queued; after three attempts they fail visibly with the reason recorded.
Decisions nobody makes expire after 7 days, and an action cut off mid-run is shown as failed so a person
can retry it safely (the target system receives the same idempotency key).

**Tests.** `pytest` runs on SQLite. To run the same suite on Postgres, as in production, point it at an
empty database: `WORKFORCE_TEST_DATABASE_URL=postgresql+psycopg://user:pw@host:5432/test pytest`.

**Browser tests.** `tests/test_e2e_browser.py` starts the real server and drives headless Chrome
(`tests/e2e/browser.mjs`, Node's built-in WebSocket, no packages) through the ways real people broke the app in live
testing: a second tab that is out of date, signing out or switching account in another tab, a session that ended,
no internet, numbers outside the allowed range, typing while the page updates, and links to things that do not
exist. It is skipped when Chrome or Node is not installed; set `CHROME_BIN` to use another Chrome or Chromium.
A test also builds the schema from the migrations and checks it matches the code exactly.

## Backup and disaster recovery

Everything the platform knows is in one database: source records exactly as received, findings,
evidence, decisions, actions and the hash-chained audit log. Tenant packs live in `tenants/` and
belong in version control. Secrets live only in `.env` (keep a copy in your password manager). AI keys
saved in the panel are in the database, encrypted with `WORKFORCE_SECRET_KEY`: back up that secret with
`.env`, or those keys will have to be entered again after a restore.

**Back up (Postgres, daily at least, kept off the server):**

The `backup` service in `docker-compose.yml` writes one such dump a day to `./backups` and keeps 14 days.
Copy them off the server. To take one by hand:

```bash
docker compose exec -T db pg_dump -U workforce -Fc workforce > workforce-$(date +%F).dump
```

Keep 14 daily and 12 monthly copies, encrypted, in a different location from the server. For a
recovery point under a day, use your provider's point-in-time recovery or WAL archiving.

**Restore:**

```bash
docker compose stop api worker
docker compose exec -T db pg_restore -U workforce -d workforce --clean --if-exists < workforce-YYYY-MM-DD.dump
docker compose run --rm migrate
docker compose start api worker
docker compose exec api workforce verify-audit --tenant <id>
```

`verify-audit` must say the chain is intact. A broken chain means the restored copy was altered or
is incomplete: use an earlier backup. After a restore, sources re-sync on their own schedule, and
work that was mid-investigation is re-queued when its lease expires.

**SQLite (single-machine pilots only):** `sqlite3 workforce.db ".backup workforce-$(date +%F).db"`.

**Test the restore** once a month into a spare database, and run `verify-audit` there.
Targets to agree in Phase 0: how much data may be lost (recovery point, default 24 hours) and how
long the service may be down (recovery time, default 4 hours).
