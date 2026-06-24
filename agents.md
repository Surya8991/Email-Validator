# AI Email Validator — Agent Context

## Overview
FastAPI web app that validates emails via multiple providers (Bouncify, ZeroBounce, NeverBounce, Hunter.io) plus a free local stack (syntax + MX + disposable + SMTP). Single-email and bulk CSV modes. Deployed on Vercel (Hobby) with Neon PostgreSQL for persistent storage. Bulk CSV jobs are offloaded to GitHub Actions to bypass Vercel's 10s function timeout.

## Stack
- **Backend:** FastAPI + Python 3.12 + uvicorn (async)
- **HTTP:** httpx.AsyncClient (shared, lifespan-managed)
- **Local validation:** email-validator, dnspython, disposable-email-domains
- **Storage:** SQLModel + **PostgreSQL (Neon)** — persistent. SQLite used locally when DATABASE_URL is unset.
- **Frontend:** HTMX + Tailwind CDN + Jinja2 templates (no build step)
- **Config:** pydantic-settings + .env
- **Serverless:** Mangum ASGI adapter for Vercel
- **Bulk processing:** GitHub Actions workflow (`bulk_process.yml`) — no timeout limit
- **Tests:** pytest + pytest-asyncio + respx
- **Lint/types:** ruff (ruff.toml) + mypy (mypy.ini)

## Key Dirs & Files
```
app/
  main.py          # FastAPI app, lifespan, custom /docs route
  config.py        # Settings (pydantic-settings) — reads .env
  db.py            # SQLModel engine + URL normalization (postgres:// → postgresql+psycopg2://)
  models.py        # DB tables: Job, EmailResult, EmailCache, ApiUsage
  schemas.py       # Pydantic DTOs (request/response)
  providers/       # base.py, bouncify.py, zerobounce.py, neverbounce.py, hunter.py, local.py, registry.py
  core/            # validator.py (strategies), csv_io.py, cache.py, retry.py
  routes/          # ui.py, api_single.py, api_bulk.py, api_stats.py, health.py
  workers/         # bulk_worker.py (BackgroundTasks fallback for local dev)
  templates/       # Jinja2 HTML (base, dashboard, validate, cache, analytics, settings, jobs)
api/
  index.py         # Mangum handler for Vercel (sys.path guard + handler = Mangum(app))
scripts/
  init_db.py       # One-time Neon table creation — run once per new DB
  process_job.py   # GitHub Actions bulk processor — reads job.csv_data from DB
  pre_push_check.sh # 26-check safety checklist (auto-runs via .githooks/pre-push)
.github/
  workflows/
    bulk_process.yml  # workflow_dispatch: triggered by api_bulk.py with job_id
.githooks/
  pre-push         # Thin wrapper calling scripts/pre_push_check.sh
ruff.toml          # Ruff config (replaces pyproject.toml [tool.ruff])
pytest.ini         # pytest config: asyncio_mode=auto, testpaths=tests
mypy.ini           # mypy strict config
.python-version    # "3.12" — controls Vercel Python version
```

## How to Run (Local)
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in BOUNCIFY_API_KEY at minimum
# If using Neon (set DATABASE_URL in .env first):
python scripts/init_db.py
uvicorn app.main:app --reload
```
Visit http://localhost:8000

## How to Init Neon DB
```bash
# Add DATABASE_URL to .env first, then:
python scripts/init_db.py
```
Tables created: `job`, `emailresult`, `emailcache`, `apiusage`

## Env Vars
### Required for Vercel
- `BOUNCIFY_API_KEY` — primary provider
- `DATABASE_URL` — Neon connection string (`postgres://...` or `postgresql+psycopg2://...`)
- `GITHUB_PAT` — PAT with `Actions (write)` scope (for bulk CSV processing)
- `GITHUB_REPO` — `owner/repo` (e.g. `Layruss98266/Email-Validator`)

### Optional providers
- `ZEROBOUNCE_API_KEY`, `NEVERBOUNCE_API_KEY`, `HUNTER_API_KEY`

### Optional config
- `CACHE_TTL_DAYS=30` — default result cache lifetime
- `HTTPX_TIMEOUT=10.0` — keep ≤ 8 on Vercel Hobby
- `MAX_BULK_EMAILS=0` — hard cap on CSV rows (0 = unlimited)
- `ENABLE_SMTP_PROBE=false` — SMTP RCPT TO probe (port 25 often blocked)
- `SMTP_PROBE_FROM` — FROM address for SMTP probes
- `*_DAILY_CAP` — per-provider daily quota cap (0 = unlimited)

### GitHub repo secrets (for bulk workflow)
- `DATABASE_URL`, `BOUNCIFY_API_KEY`, optional provider keys

## Providers & Verdicts
All normalize to: `valid | invalid | risky | unknown`
- Bouncify: `deliverable→valid`, `undeliverable→invalid`, `accept_all|unknown→risky`
- ZeroBounce: `valid→valid`, `invalid→invalid`, `catch-all/abuse/do_not_mail→risky`
- NeverBounce: `valid→valid`, `invalid→invalid`, `disposable/catchall→risky`
- Hunter: `valid→valid`, `invalid→invalid`, `accept_all/disposable→risky`
- Local: syntax + MX + disposable-email-domains + role prefix checks

## Strategies
- `bouncify_only` — single provider, cheapest
- `local_first` — local check first; skip paid API on obvious invalids
- `consensus` — all enabled providers in parallel, majority vote
- `waterfall` — local → hunter → bouncify → zerobounce (stop at first confident result)

## Bulk CSV Flow
1. User uploads CSV → `POST /api/bulk` stores `csv_data` in DB, creates Job row
2. Vercel function calls GitHub Actions `workflow_dispatch` API with `job_id`
3. GHA runner: `python scripts/process_job.py --job-id <id>` reads DB, validates, writes results
4. Frontend polls `GET /api/bulk/{id}` for progress
5. User downloads `GET /api/bulk/{id}/download?verdict=all|valid|invalid|risky`

## Cache TTL Semantics
- `ttl_days=None` → use global `CACHE_TTL_DAYS` setting
- `ttl_days=0` → skip caching entirely
- `ttl_days=N` → cache for N days

## Vercel Deployment Notes
- **No `pyproject.toml`** — Vercel runs `uv lock` on any pyproject.toml and fails. Config split into `ruff.toml` + `pytest.ini` + `mypy.ini`.
- **`.python-version`** controls Python version (must be `3.12`)
- **`vercel.json`**: `"maxDuration": 10` (Hobby limit)
- **`api/index.py`**: `sys.path.insert(0, root)` guard + `handler = Mangum(app, lifespan="auto")`
- **Jinja2Templates**: must use absolute `Path(__file__).parent.parent / "templates"` — relative paths break in Vercel
- **SQLite on Vercel**: ephemeral `/tmp/` — data lost on cold starts. Always use DATABASE_URL for production.

## Sensitive / Gotchas
- `.env` is gitignored — never commit API keys
- SMTP probe off by default — port 25 blocked on most cloud/ISP
- Per-provider daily caps prevent accidental credit burn
- `disposable-email-domains` needs occasional `pip install -U`
- `job.csv_data` stores raw CSV in DB — required for GitHub Actions to read it (no shared filesystem)
- Download endpoint generates CSV from `EmailResult` DB rows (no disk file — survives Vercel cold starts)

## Run Tests
```bash
pytest -q
```
All tests mock external HTTP (respx) — no real API calls in CI.

## Pre-push Check
```bash
bash scripts/pre_push_check.sh
# runs automatically via git hook (install once):
git config core.hooksPath .githooks
```
26 checks across 7 groups: tests, lint, secrets, Vercel config, GitHub Actions, debug debris, critical files.
