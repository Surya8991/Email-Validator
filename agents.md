# AI Email Validator — Agent Context

## Overview
FastAPI web app that validates emails via multiple providers (Bouncify, ZeroBounce, NeverBounce, Hunter.io) plus a free local stack (syntax + MX + disposable + SMTP). Single-email, bulk-CSV, **bulk-XLSX**, and **paste-emails** modes. Deployed on Vercel (Hobby) with Neon PostgreSQL for persistent storage. Bulk jobs are offloaded to GitHub Actions to bypass Vercel's 10s function timeout. SMTP transactional email for invites, approvals, password reset, and team-join decisions.

Current version: **0.10.3** (verify_bulk rewritten to match real Bouncify 5-endpoint flow; opt-in via `BOUNCIFY_BULK=1`. See PROJECT_LOG.md Session 17.)

## Stack
- **Backend:** FastAPI + Python 3.12 + uvicorn (async)
- **HTTP:** httpx.AsyncClient (shared, lifespan-managed)
- **Auth:** Session-based (HttpOnly cookie `ev_session`), SHA-256 hashed tokens, 7-day sliding TTL. `bcrypt` library directly (passlib incompatible with bcrypt>=5). **Failed-login lockout**: 5 wrong attempts → `User.locked_until` set 15 min ahead, returns 429 until expiry. **Per-IP rate limit** (`app/security/rate_limit.py`) on `/login` (10/60s), `/forgot-password` + `/register` (5/300s) — closes the login-enumeration gap where unknown emails bypassed the per-account lockout. Password change + reset revoke **all** existing sessions and issue a fresh cookie.
- **Security middleware:** `SecurityHeadersMiddleware` in `app/main.py` sets `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, and HSTS (prod-only). Same middleware does an Origin/Referer cross-host check on every non-safe method — lightweight CSRF defence on top of `samesite="lax"`.
- **Email:** stdlib `smtplib` in `app/services/email.py`, async via `asyncio.to_thread`. Gmail-friendly STARTTLS (587) or SMTPS (465). Every send is failure-isolated — `SMTP_HOST=""` silently disables all mail.
- **Local validation:** email-validator, dnspython, disposable-email-domains
- **Storage:** SQLModel + **PostgreSQL (Neon)** — persistent. SQLite used locally when DATABASE_URL is unset.
- **Frontend:** HTMX + Tailwind CDN + Jinja2 templates (no build step)
- **Config:** pydantic-settings + .env
- **Serverless:** Vercel native Python runtime (auto-detects ASGI — no Mangum)
- **Bulk processing:** GitHub Actions workflow (`bulk_process.yml`) — no timeout limit. Triggered INLINE from `/api/bulk` (Vercel kills BackgroundTasks the moment the response returns).
- **Keep-warm:** GitHub Actions cron — three redundant workflows (`keep_warm.yml` + `keep_warm_b.yml` + `keep_warm_c.yml`) all targeting the same 5-min grid at offsets `{2, 1, 4}` minutes. All three share `concurrency: keep-warm` with `cancel-in-progress: true` so near-simultaneous fires collapse to ONE ping. Multiplies fire-probability per window without multiplying ping traffic. **Do NOT tighten below 5 min** — GitHub coalesces/deprioritizes denser schedules. **Do NOT delete the redundant files** thinking they're duplicates. For a true SLA on warmth, configure UptimeRobot (free, 5-min HTTP monitor) — GHA's scheduler is best-effort on free tier and 5-min-cadence × 24h × 30d would technically exceed the 2,000-min private-repo quota if it actually fired every time.
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
  security/        # rate_limit.py — in-memory per-IP token bucket (no Redis dep)
  services/        # email.py — SMTP mailer + 4 transactional templates (invite/approval/reset/team-join). User-supplied fields html-escaped before interpolation.
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
    bulk_process.yml  # workflow_dispatch: triggered by api_bulk.py with job_id. Receives inputs.job_id via env var (JOB_ID), not shell-interpolated.
    keep_warm.yml     # cron every 5 min: curls ${{ vars.APP_URL }}/api/health to keep Neon + Vercel function warm
    ci.yml            # PR + push to main: ruff check, mypy app (non-blocking), pytest -q
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
- `GITHUB_REPO` — `owner/repo` of the worker repo (blank by default — `.env.example` no longer hardcodes the upstream repo so forks don't dispatch to it).
- `JOB_CALLBACK_TOKEN` — shared secret that the `bulk_process` workflow uses to call `/api/bulk/{id}/workflow-callback` when a run finishes. Must match the GitHub repo secret of the same name. Without this set, the callback endpoint returns 503 and jobs cancelled in the GitHub UI stay `running` forever in the app. Generate: `python -c "import secrets; print(secrets.token_hex(16))"`.
- `SECRET_KEY` — random string for session signing (generate: `openssl rand -hex 32`)
- `BASE_URL` — canonical public origin (e.g. `https://validator.example.com`). Used for all outbound links (password reset, invites, approvals). **Must be set in production** — never trust the request `Host` header behind a proxy. Falls back to `request.base_url` for local dev only.

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
- `MAX_BULK_EMAILS=1000` — hard cap on rows per single CSV upload (0 = unlimited). Bigger files get a 400 with "exceeds N email limit per upload".
- `MAX_USER_ACTIVE_JOBS=4` — max queued+running bulk jobs a single user can have in flight (0 = unlimited). Excess uploads get a 429.
- `MAX_USER_ACTIVE_EMAILS=2000` — max sum of pending emails across a user's queued+running jobs (0 = unlimited). A new upload that would push the user over the cap gets a 429.
- The bulk_process workflow caps concurrent runs at 10 via a 10-bucket `concurrency:` group keyed on the last digit of `inputs.job_id` (bulk_process) or `inputs.bucket` (retry_unknowns). GHA expression syntax has no `%` operator, so the buckets are spelled out via a chain of `endsWith()` checks. An 11th dispatched job queues at GitHub-Actions level until a slot frees. Bouncify rate limits may inflate `unknown` at this concurrency on lower tiers — re-resolve those via the retry-unknowns sweep.
- Workflow concurrency knobs (repo variables, all optional — leave unset for script defaults): `CHUNK_SIZE` (per-email in-flight per gather, default 20 in bulk_process / 5 in retry_unknowns), `BULK_SUB_BATCH` (bulk-path emails per submission, default 500), `BOUNCIFY_BULK` (set `1` to enable the 10×-faster bulk API path for `bouncify_only` / `local_first` jobs; default off pending a confidence-building 1k-row comparison run), `PROGRESS_EVERY` (emails between progress log lines, default 50 — applies to both workflows).
- Both `scripts/process_job.py` and `scripts/retry_unknowns.py` log the same progress shape: `done/total (X%) | valid=A invalid=B risky=C unknown=D | rate emails/s` every `PROGRESS_EVERY` emails, so a long bulk run is observable in real time rather than a chunk-by-chunk wall of text.
- `scripts/retry_unknowns.py` + `.github/workflows/retry_unknowns.yml` re-validate `EmailResult.verdict='unknown'` rows in batches (default 500). Args: `--batch-size`, `--max-batches`, `--job-id`, `--since-days`, `--providers`, `--strategy`, `--strikes`. UPDATEs every emailresult row for the email when a real verdict is reached, writes the cache, and increments `retry_count` on rows that come back unknown again.
- **3-strikes rule**: `EmailResult.retry_count` (created by the `_PG_COLUMN_ADDS` startup migration as `INTEGER DEFAULT 0 NOT NULL`) tracks how many times retry_unknowns has re-validated each row. Once a row's `retry_count` reaches `--strikes` (default 3, env `UNKNOWN_STRIKES`) AND the latest verdict is still 'unknown', the script flips that row's verdict to 'invalid' so it leaves the retry pool. Persistent unknowns are dead-MX / parked domains in practice — treating them as invalid stops re-burning Bouncify credits forever. The retry SELECT also filters `retry_count < strikes`, so struck-out rows are immediately ineligible for the next sweep.
- `POST /admin/retry-unknowns` (admin-only) **fans out** the retry sweep across `num_buckets` parallel workflow dispatches (default 15, max 20). Each dispatch processes one hash bucket — same email always lands in the same bucket via `MOD(ABS(HASHTEXT(LOWER(email))), :of) = :bucket`, so parallel runs never double-process. GHA's 3-bucket concurrency group on `retry_unknowns.yml` caps in-flight at 3; the rest wait in GitHub's queue and dequeue as runs finish. Query params: `batch_size`, `max_batches` (default 1 — one bucket per workflow), `since_days`, `providers`, `strategy`, `job_id`, `strikes`, `num_buckets`. Admin stats page button fires `?batch_size=500&max_batches=1&num_buckets=15` for a ~7,500-email sweep per click.
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

## Loading / ETA (0.9.3)
- `base.html` ships a global CSS-only HTMX progress bar (`#hx-progress`).
  Every `hx-*` request fades it in — no per-template wiring needed.
- Per-row dim: `tr:has(.htmx-request)` greys + locks the row while a
  delete is in flight.
- Inline button spinner: drop `<span class="htmx-indicator hx-spin"></span>`.
- ETA on jobs: `app/templating.py` exposes `humanize_duration` (Jinja
  filter `duration`) and `job_eta_seconds(processed, total, started_at)`.
  Used by `partials/job_progress.html`; computed from `Job.created_at`
  to avoid a `started_at` migration.
- `/jobs` auto-polls every 5s when any row is queued/running, via
  `hx-get` + `hx-select=".card"`.

## Delete endpoints (0.9.2)
- `DELETE /api/bulk/{id}` — admin-only. Deletes a Job + its EmailResult rows. 409 if `status='running'`, 403 for non-admins (was owner-or-admin before; locked down so end-users can't wipe their own — or anyone else's — history).
- `POST /api/bulk/clear` — admin-only. Deletes all non-running jobs.
- `DELETE /api/cache/{id}` — auth required (was anonymous before 0.9.2).
- `POST /api/cache/purge` — auth required. Deletes expired rows only.
- `POST /api/cache/clear` — admin-only. Wipes the entire cache.

UI buttons live on `/jobs` (per-row + "Clear all history" header), `/jobs/{id}` (Delete job), and `/cache` ("Clear all" next to "Purge Expired").

## Retry + workflow callback
- `POST /api/bulk/{id}/retry` — owner-or-admin. Only valid when `status='failed'`. Deletes existing `EmailResult` rows for the job (the worker iterates the whole CSV every run, so leftovers would duplicate), resets `status/processed/error`, re-dispatches the workflow with `triggered_by=current_user.email`. Returns 410 if `csv_data` has been pruned. Retry buttons live on `/jobs` rows and `/jobs/{id}` for failed jobs.
- `POST /api/bulk/{id}/workflow-callback` — called by the workflow's final `if: always()` step. Auth via `X-Callback-Token` header matched against `JOB_CALLBACK_TOKEN`. Body: `{conclusion, run_url, reason?}`. Flips the job to `failed` (with run URL embedded in `job.error`, rendered as a link by the `linkify` filter) when the run was cancelled in the GitHub UI, killed by the runner, or timed out — cases where `_mark_failed` inside the script never ran. Refuses to clobber jobs already `done` or `failed`.
- `bulk_process.yml` inputs: `job_id`, `cache_ttl_days`, `triggered_by`. `run-name` is `"Bulk #<id> — <email>"`. Repo variable `APP_URL` gates the notify step (no-op if unset).

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
- **Last-superadmin guard:** `/admin/users/{id}/demote` and `/deactivate` refuse to remove the last active superadmin so the system can't be left without a privileged user. Demote covers `admin → user` AND `superadmin → user`.
- **IDOR-safe bulk endpoints:** `Job.user_id` stamped on creation; `/api/bulk/{id}` status, download, and delete return 404 unless `job.user_id == current_user.id` (admin/superadmin sees all).
- **Session rotation:** password change (`/profile/password`) and password reset (`/reset-password/{token}`) revoke every existing `UserSession` row and issue a fresh cookie. Phished sessions can't outlive a reset.
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
