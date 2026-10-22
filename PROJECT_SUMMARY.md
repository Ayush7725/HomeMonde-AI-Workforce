# AI Workforce: project summary

A multi-company platform of "AI employees" for a manufacturer. Each one watches one department's data
(stock, factory, purchasing, invoices, sales, ads, logistics, HR), raises work when a business rule says
something needs attention, investigates the cause across departments with an LLM, and brings an
evidence-backed recommendation to a person, who approves or rejects it. First deployment: HomeMonde, a
home-textiles manufacturer in Jaipur.

## What was built

| Part | Size |
|---|---|
| Platform (Python, FastAPI, SQLAlchemy, Alembic) | ~7,000 lines in 28 modules |
| Web app (vanilla JS, no framework): a business dashboard for founders and managers (Home, Tasks, Decisions, Alerts, Reports, AI team, settings) plus an animated live floor | ~2,600 lines |
| Tests (pytest; the Claude API is never called) | 190 tests (incl. a real headless-Chrome test of 10 user-break scenarios), ~3,900 lines |
| Company configuration (tenant packs: 1 real company, 3 demo companies) | ~2,200 lines of YAML |
| HomeMonde pack | 10 AI employees, 12 datasets, 65 metrics, 14 rules, 20 tools, 6 approvable actions |
| Deployment | Docker image, Compose (Postgres, migrations, API, worker, daily backup) and a one-command demo |

## Design decisions worth talking about in an interview

- **The AI is never the source of truth.** A deterministic metric engine computes every number, and the LLM only explains.
  Every number in an AI statement must appear in the evidence it cites, or the statement is removed.
- **Rules create work, not the model.** Thresholds are versioned in config. Rules never fire on stale data. Alerts close by
  themselves when the problem clears, and batched alerts track which items are back to normal.
- **Bad data is visible, never guessed.** Each file is validated row by row. A file with more than 5% bad rows is refused
  whole. Contradicting rows are left out and reported. An unknown value is shown as unknown, never as zero. Every data
  problem becomes a task for the person who owns that export.
- **People decide.** Actions run only after approval (role, area and amount limits), then pass pre-action checks, run
  once and are verified. Every step is written to a hash-chained audit log.
- **Multi-tenant by configuration.** A new company is onboarded with a YAML pack, not code. Four demo companies with
  different data shapes (CSV and SQL, Indian, US and UK formats) run side by side without mixing.
- **Model-agnostic, with cost controls.** Anthropic, OpenAI, Gemini, DeepSeek and Hugging Face sit behind one adapter,
  with a model tier per task, daily token and cost budgets, and reuse of earlier explanations when figures barely moved.

## How it was tested

1. **Real company data.** 444,000 Amazon and Shopify order lines, plus stock files. This found 9 bugs, all fixed
   generically.
2. **Generated stress test.**
   - **Dataset:** 766,700 order lines, 232,000 shipments and 12 linked files from 12 systems, with 23 planted
     business situations, 26 near-misses sitting exactly on a threshold, about 1% dirty rows, 8 broken files and a
     next-day folder. The answer key was checked independently first.
   - **Before fixes:** 82/90 alerts found, 42 false alarms.
   - **After fixes:** 90/90 alerts found, 26/26 near-misses quiet, 0 false alarms, 99.3% of 22,644 figures exact
     (the remainder is a deliberate ₹-parsing choice), 8/8 broken files handled, and 14/14 alerts closed by
     themselves the next day.
   - **Speed:** the full load takes about 6 minutes on SQLite. A 250,000-row stock snapshot loads in about 1 minute.

3. **Live AI run (free Gemini key).** An agent traced the HomeMonde stockout across departments: low stock, then the
   blocked batch, then the late fabric order. It recommended an emergency order from the faster approved supplier, and every
   number cited its evidence. Rate limits are handled: the agent waits as long as the provider asks, and a used-up daily
   quota falls back to facts-only findings.

## Run the demo

```bash
docker compose -f docker-compose.demo.yml up --build     # http://127.0.0.1:8100, password demo-workforce-2026
```

Sign in as `founder@homemonde.demo`. AI is optional: without a key, employees report verified facts only.

## Résumé lines

- Built a multi-tenant "AI workforce" platform (Python, FastAPI, Postgres, Docker) in which 10 LLM-backed agents
  monitor a manufacturer's sales, stock, factory, purchasing and finance data and bring evidence-backed recommendations
  to managers for approval.
- Designed a deterministic metric and rules engine plus a grounding validator, so every number an agent states is
  computed by code and traceable to source records; multi-provider LLM layer (Claude, Gemini, OpenAI) with cost
  budgets and model routing.
- Validated it on 444k real order lines and a 766k-row adversarial dataset with planted near-misses: 90/90 issues
  detected, 0 false alarms, 99.3% of 22,644 figures exact; 190 automated tests incl. real-browser checks; one-command Docker demo.
