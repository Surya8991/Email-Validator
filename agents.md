# AI Email Validator — Agent Context

## Overview
FastAPI web app that validates emails via multiple providers (Bouncify, ZeroBounce, NeverBounce, Hunter.io) plus a free local stack (syntax + MX + disposable + SMTP). Single-email, bulk-CSV, **bulk-XLSX**, and **paste-emails** modes. Deployed on Vercel (Hobby) with Neon PostgreSQL for persistent storage. Bulk jobs are offloaded to GitHub Actions to bypass Vercel's 10s function timeout. SMTP transactional email for invites, approvals, password reset, and team-join decisions.

## Stack
- **Backend:** FastAPI + Python 3.12 + uvicorn (async)
- **HTTP:** httpx.AsyncClient (shared, lifespan-managed)
- **Auth:** Session-based (HttpOnly cookie `ev_session`), SHA-256 hashed tokens, 7-day sliding TTL. `bcrypt` library directly (passlib incompatible with bcrypt>=5). **Failed-login lockout**: 5 wrong attempts → `User.locked_until` set 15 min ahead, returns 429 until expiry.
- **Email:** stdlib `smtplib` in `app/services/email.py`, async via `asyncio.to_thread`. Gmail-friendly STARTTLS (587) or SMTPS (465). Every send is failure-isolated — `SMTP_HOST=""` silently disables all mail.
- **Local validation:** email-validator, dnspython, disposable-email-domains
- **Storage:** SQLModel + **PostgreSQL (Neon)** — persistent. SQLite used locally when DATABASE_URL is unset.
- **Frontend:** HTMX + Tailwind CDN + Jinja2 templates (no build step)
- **Config:** pydantic-settings + .env
- **Serverless:** Vercel native Python runtime (auto-detects ASGI — no Mangum)
- **Bulk processing:** GitHub Actions workflow (`bulk_process.yml`) — no timeout limit. Triggered INLINE from `/api/bulk` (Vercel kills BackgroundTasks the moment the response returns).
- **Keep-warm:** GitHub Actions cron (`keep_warm.yml`) every 3 min hitting `/api/health` → keeps Neon (5-min auto-pause) and the Vercel function warm. Schedule offset off the hour grid to dodge scheduler congestion.
- **Tests:** pytest + pytest-asyncio + respx
- **Lint/types:** ruff (ruff.toml) + mypy (mypy.ini)

## Key Dirs & Files
```
app/
  main.py          # FastAPI app, lifespan, exception handlers (RequiresAuth/RequiresAdmin), bootstrap admin
  config.py        # Settings (pydantic-settings) — reads .env
  auth.py          # Session helpers: create/delete/get session, require_auth/admin/superadmin guards
  db.py            # SQLModel engine + URL normalization (postgres:// → postgresql+psycopg2://)
  models.py        # DB tables: Job, EmailResult, EmailCache, ApiUsage, User, UserSession, Team, TeamMembership (with role: owner|member), UserInvite, PasswordReset, AuditLog, SystemSetting
  schemas.py       # Pydantic DTOs (request/response)
  providers/       # base.py, bouncify.py, zerobounce.py, neverbounce.py, hunter.py, local.py, registry.py
  services/        # email.py — SMTP mailer + 4 transactional templates (invite/approval/reset/team-join)
  core/            # validator.py (strategies), csv_io.py, cache.py, retry.py
  routes/
    ui.py          # User-facing UI (auth-gated), /teams + join/cancel. Dashboard `/` has 30s in-process cache + 6s timeout on aggregates.
    auth_routes.py # /login (lockout-aware), /register (notifies admins), /logout, /invite/{token}, /forgot-password, /reset-password/{token}, /profile + /profile/{email,password,sessions/revoke-all}
    admin.py       # /admin/* — users (search/filter/invite/limit), audit-log + export, sessions, sys-settings, teams (owner role, transfer, edit, delete), stats, usage, providers
    api_single.py, api_bulk.py (xlsx-aware, paste path), api_stats.py (cache export), health.py (now SELECT 1)
  workers/         # bulk_worker.py (BackgroundTasks fallback — local dev ONLY; gated on `not os.getenv("VERCEL")`)
  templates/
    base.html      # Main nav (Lucide SVG icons, backdrop-blur, underline active state, avatar dropdown + admin tab)
    auth/          # login.html, register.html (split-panel design)
    admin/         # base.html (sectioned sidebar: Data/Access/Config/Superadmin), users.html (search+filter+invite+limit, invite-email status), stats.html (A6 dashboard), audit_log.html (+ Export CSV), sessions.html, sys_settings.html, usage.html, providers.html, teams.html, team_detail.html (Owner badge, Make-owner button, Edit modal)
    auth/          # login.html (Forgot-password link), register.html, invite.html, forgot_password.html, reset_password.html, profile.html
    teams.html     # User-facing team cards with join/cancel request
api/
  index.py         # Vercel entry — sys.path guard + `from app.main import app` (ASGI auto-detected)
scripts/
  init_db.py       # One-time Neon table creation — run once per new DB
  process_job.py   # GitHub Actions bulk processor — reads job.csv_data from DB
  pre_push_check.sh # 38-check safety checklist (auto-runs via .githooks/pre-push)
.github/
  workflows/
    bulk_process.yml  # workflow_dispatch: triggered by api_bulk.py with job_id
    keep_warm.yml     # cron */3min: curls ${{ vars.APP_URL }}/api/health to keep Neon + Vercel function warm
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
Tables created: `job`, `emailresult`, `emailcache`, `apiusage`, `user`, `usersession`, `team`, `teammembership`, `userinvite`, `auditlog`, `systemsetting`

## Env Vars
### Required for Vercel
- `BOUNCIFY_API_KEY` — primary provider
- `DATABASE_URL` — Neon connection string (`postgres://...` or `postgresql+psycopg2://...`)
- `GITHUB_PAT` — fine-grained PAT, Actions: read/write (for bulk CSV processing). Without this, jobs sit at `queued` forever on Vercel.
- `GITHUB_REPO` — `owner/repo` (default: `Surya8991/Email-Validator`)
- `SECRET_KEY` — random string for session signing (generate: `openssl rand -hex 32`)

### SMTP (outbound mail — Gmail recommended)
- `SMTP_HOST` (e.g. `smtp.gmail.com`) — leave blank to disable all email
- `SMTP_PORT` (default `587`; use `465` for SSL — code auto-switches)
- `SMTP_USER` / `SMTP_PASSWORD` (for Gmail: an **App Password**, not the regular password)
- `SMTP_USE_TLS=true`
- `SMTP_FROM` — must equal `SMTP_USER` on Gmail
- `SMTP_FROM_NAME=Email Validator`

### Auth bootstrap (set once for production)
- `ADMIN_EMAIL` / `ADMIN_PASSWORD` — creates first admin user if User table is empty
- `SUPERADMIN_EMAIL` — promoted/created as superadmin on every app startup (idempotent); superadmin can create/promote admins

### Optional providers
- `ZEROBOUNCE_API_KEY`, `NEVERBOUNCE_API_KEY`, `HUNTER_API_KEY`

### Optional config
- `CACHE_TTL_DAYS=30` — default result cache lifetime
- `HTTPX_TIMEOUT=10.0` — keep ≤ 8 on Vercel Hobby
- `MAX_BULK_EMAILS=0` — hard cap on CSV rows (0 = unlimited)
- `ENABLE_SMTP_PROBE=false` — SMTP RCPT TO probe (port 25 often blocked)
- `SMTP_PROBE_FROM` — FROM address for SMTP probes
- `*_DAILY_CAP` — per-provider daily quota cap (0 = unlimited)
- `PRODUCTION=true` — enables stricter security defaults

### GitHub repo secrets (for bulk_process.yml worker)
- `DATABASE_URL`, `BOUNCIFY_API_KEY`, optional provider keys

### GitHub repo variables (different from secrets — Variables tab)
- `APP_URL` — full origin without trailing slash (e.g. `https://email-validator-lilac.vercel.app`). Required for `keep_warm.yml`.
- `CACHE_TTL_DAYS` — e.g. `30`. Empty values are tolerated by `env_ignore_empty=True` on `SettingsConfigDict` in `app/config.py` (0.9.1+).

## Providers & Verdicts
All normalize to: `valid | invalid | risky | unknown`
- Bouncify: `deliverable→valid`, `undeliverable→invalid`, `accept_all|unknown→risky`
- ZeroBounce: `valid→valid`, `invalid→invalid`, `catch-all/abuse/do_not_mail→risky`
- NeverBounce: `valid→valid`, `invalid→invalid`, `disposable/catchall→risky`
- Hunter: `valid→valid`, `invalid→invalid`, `accept_all/disposable→risky`
- Local: syntax + MX + disposable-email-domains + role prefix checks

## Strategies
- `bouncify_only` — free local syntax+MX pre-filter (skips Bouncify on hard invalids), then Bouncify for the rest. Cheapest single-provider path.
- `local_first` — local check first; skip paid API on obvious invalids
- `consensus` — all enabled providers in parallel, majority vote
- `waterfall` — local → hunter → bouncify → zerobounce (stop at first confident result)

## Delete endpoints (0.9.2)
- `DELETE /api/bulk/{id}` — deletes a Job + its EmailResult rows. 409 if `status='running'`.
- `POST /api/bulk/clear` — admin-only. Deletes all non-running jobs.
- `DELETE /api/cache/{id}` — auth required (was anonymous before 0.9.2).
- `POST /api/cache/purge` — auth required. Deletes expired rows only.
- `POST /api/cache/clear` — admin-only. Wipes the entire cache.

UI buttons live on `/jobs` (per-row + "Clear all history" header), `/jobs/{id}` (Delete job), and `/cache` ("Clear all" next to "Purge Expired").

## Bulk Upload Flow
1. User uploads **CSV** / **XLSX** / pasted emails → `POST /api/bulk`. XLSX is converted to CSV server-side via openpyxl; paste mode is converted client-side to a `pasted.csv` blob.
2. `Job` row created, `csv_data` stored in DB.
3. Vercel function calls GitHub Actions `workflow_dispatch` API INLINE (4s timeout) with `job_id` — must finish before the response returns because Vercel kills the function after that.
4. GHA runner: `python scripts/process_job.py --job-id <id>` reads DB, validates, writes results.
5. Frontend polls `GET /api/bulk/{id}` for progress.
6. User downloads `GET /api/bulk/{id}/download?verdict=all|valid|invalid|risky`.

Templates: `GET /api/bulk/template.csv` and `GET /api/bulk/template.xlsx` (openpyxl-generated on the fly).

## Cache TTL Semantics
- `ttl_days=None` → use global `CACHE_TTL_DAYS` setting
- `ttl_days=0` → skip caching entirely
- `ttl_days=N` → cache for N days

## Vercel Deployment Notes
- **No `pyproject.toml`** — Vercel runs `uv lock` on any pyproject.toml and fails. Config split into `ruff.toml` + `pytest.ini` + `mypy.ini`.
- **`.python-version`** controls Python version (must be `3.12`)
- **`vercel.json`**: `"maxDuration": 10` (Hobby limit)
- **`api/index.py`**: `sys.path.insert(0, root)` guard + `from app.main import app` — Vercel auto-detects the ASGI app. **Do NOT use Mangum** (it produces AWS Lambda response shape and Vercel returns `FUNCTION_INVOCATION_FAILED`).
- **Lifespan schema migrations**: `app/db.py:_apply_lightweight_migrations()` runs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for any entry in `_PG_COLUMN_ADDS` on every cold start. Idempotent, Postgres-only. Append to this list whenever a new column is added to a model — `create_all` will not alter existing tables.
- **Jinja2Templates**: import `templates` from `app.templating` (0.9.2+) — single shared instance with the `ist` UTC→IST filter pre-registered. Routes no longer construct their own `Jinja2Templates(...)`.
- **SQLite on Vercel**: ephemeral `/tmp/` — data lost on cold starts. Always use DATABASE_URL for production.

## Auth Architecture
- **Roles:** `user` → `admin` → `superadmin` (three-tier; each tier inherits lower permissions)
- **Session tokens:** raw token in `ev_session` HttpOnly SameSite=Lax cookie; SHA-256 hash stored in DB only
- **`require_auth`**: raises `RequiresAuth` exception → Starlette handler redirects to `/login` (can't return RedirectResponse from FastAPI Depends)
- **`require_admin`**: allows `admin` or `superadmin` roles
- **`require_superadmin`**: strict — `superadmin` only (promote/demote actions)
- **Teams flow:** admin creates team → user requests join (`/teams/{id}/request`) → admin approves/rejects (`/admin/teams/{id}/approve|reject/{mid}`, both now email the user when SMTP is configured)
- **Team ownership:** creator is auto-added as owner (`TeamMembership.role="owner"`); ownership transferrable to any active member via `POST /admin/teams/{id}/transfer/{user_id}`; owner cannot be removed (must transfer first or delete the team); `backfill_team_owners()` in `app/db.py` retro-adds owner rows for legacy teams on startup
- **bcrypt directly** — `passlib[bcrypt]` raises ValueError during backend init with bcrypt>=5; use `bcrypt>=4.0.0` and call `bcrypt.hashpw`/`checkpw` directly
- **`UserSession` model** — named to avoid conflict with `sqlmodel.Session`
- Data is shared across all users (no per-user isolation) — auth is access control only

## Sensitive / Gotchas
- `.env` is gitignored — never commit API keys, SECRET_KEY, or SMTP_PASSWORD
- SMTP probe (validation feature) off by default — port 25 blocked on most cloud/ISP
- Per-provider daily caps prevent accidental credit burn
- `disposable-email-domains` needs occasional `pip install -U`
- `job.csv_data` stores raw CSV in DB — required for GitHub Actions to read it (no shared filesystem). 0.9.2 hot paths (`/jobs`, `/jobs/{id}`, `/jobs/{id}/status`, dashboard `recent_jobs`) project columns explicitly to avoid pulling csv_data over the wire — that was 504-ing list pages on cold Neon.
- Download endpoint generates CSV from `EmailResult` DB rows (no disk file — survives Vercel cold starts)
- Registered users start with `is_active=False` — an admin must activate them before they can log in
- **Vercel + BackgroundTasks**: the Python serverless runtime kills the function process immediately after the response is sent. FastAPI `BackgroundTasks` do NOT run reliably. Any post-response work that must happen on Vercel must instead run inline before the response.
- **Empty env values**: an empty string for an int/float setting (e.g. `CACHE_TTL_DAYS=""`) used to crash startup. Fixed in 0.9.1 by `env_ignore_empty=True` in `app/config.py`'s `SettingsConfigDict`. Do NOT replace this with a `model_validator(mode="before")` — pydantic-settings merges env values AFTER before-validators, so they don't fire for env sources.
- **Cold-start chain**: with both Vercel and Neon on free tier, after 5 min idle every page load can 10s+ time out. The keep-warm cron is what prevents this. If you see widespread 504s, check that `keep_warm.yml` is firing and `APP_URL` is set.
- **Gmail SMTP**: `SMTP_FROM` MUST equal `SMTP_USER` or Gmail rejects. Use an App Password (regular password is blocked).
- **`session.commit()` expires ORM attributes**: if you load rows then commit something else in the same session, accessing the original rows' attributes raises `DetachedInstanceError`. Snapshot to plain tuples/dicts before commit (see audit-log export).
- **Migration list**: adding a column to a model REQUIRES appending the `(table, column, DDL)` to `_PG_COLUMN_ADDS` in `app/db.py`. New tables are auto-created by `SQLModel.metadata.create_all()`; columns on existing tables are not.

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
38 checks across 8 groups: tests, lint, secrets, Vercel config, GitHub Actions, debug debris, critical files, auth.
