# AI Email Validator — Agent Context

## Overview
FastAPI web app that validates emails via multiple providers (Bouncify, ZeroBounce, NeverBounce, Hunter.io) plus a free local stack (syntax + MX + disposable + SMTP). Single-email and bulk CSV modes.

## Stack
- **Backend:** FastAPI + uvicorn (async)
- **HTTP:** httpx.AsyncClient (shared, lifespan-managed)
- **Local validation:** email-validator, dnspython, disposable-email-domains
- **Storage:** SQLite + SQLModel (MVP; swap to Postgres later)
- **Frontend:** HTMX + Tailwind CDN + Jinja2 templates (no build step)
- **Config:** pydantic-settings + .env
- **Tests:** pytest + pytest-asyncio + respx

## Key Dirs
```
app/
  main.py          # FastAPI app
  config.py        # Settings (pydantic-settings)
  db.py            # SQLModel engine
  models.py        # DB tables: Job, EmailResult, ApiUsage
  schemas.py       # Pydantic DTOs
  providers/       # base.py, bouncify.py, zerobounce.py, neverbounce.py, hunter.py, local.py, registry.py
  core/            # validator.py (strategies), csv_io.py, retry.py
  routes/          # ui.py, api_single.py, api_bulk.py, health.py
  workers/         # bulk_worker.py (BackgroundTasks)
  templates/       # Jinja2 HTML
static/            # htmx.min.js, tailwind (CDN)
tests/             # pytest, respx mocks
uploads/           # gitignored
```

## How to Run
```bash
pip install -e ".[dev]"
cp .env.example .env  # fill in API keys
uvicorn app.main:app --reload
```
Visit http://localhost:8000

## Env Vars
- `BOUNCIFY_API_KEY` — required for Bouncify provider
- `ZEROBOUNCE_API_KEY`, `NEVERBOUNCE_API_KEY`, `HUNTER_API_KEY` — optional
- `ENABLE_SMTP_PROBE=false` — SMTP probe (many ISPs block port 25)
- `SMTP_PROBE_FROM` — FROM address for SMTP probes
- `*_DAILY_CAP` — per-provider daily quota cap (0=unlimited)

## Providers & Verdicts
All providers normalize to: `valid | invalid | risky | unknown`
- Bouncify: `deliverable→valid`, `undeliverable→invalid`, `accept_all|unknown→risky`
- ZeroBounce: `valid→valid`, `invalid→invalid`, `catch-all/abuse/do_not_mail→risky`
- NeverBounce: `valid→valid`, `invalid→invalid`, `disposable/catchall→risky`
- Hunter: `valid→valid`, `invalid→invalid`, `accept_all/disposable→risky`
- Local: syntax+MX+disposable+role checks

## Strategies
- `bouncify_only` — single provider, cheapest
- `local_first` — local check first; skip paid API on obvious invalids
- `consensus` — run all enabled providers in parallel, majority vote
- `waterfall` — local → hunter → bouncify → zerobounce (stop at first confident result)

## Sensitive / Gotchas
- `.env` is gitignored — never commit API keys
- SMTP probe off by default — port 25 often blocked
- Per-provider daily caps in config prevent accidental credit burn
- `disposable-email-domains` package needs occasional refresh
- Bouncify bulk: create job → poll status → download CSV (async polling)

## Run Tests
```bash
pytest -q
```
All tests mock external HTTP (respx) — no real API calls in CI.
