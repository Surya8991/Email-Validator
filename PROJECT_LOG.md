# AI Email Validator — Master Project Log

> **ACCOUNT-SWITCH PROOF. Read every section before touching any code.**
> Last updated: 2026-06-27 (Session 9). Current VERSION: **0.9.0**

---

## 60-Second Resume

```
1. cd "C:\Users\Surya L\Desktop\AI Agents\AI Email Vaildator"
2. Verify imports:  python -c "import fastapi, sqlmodel, httpx, email_validator, dns, disposable_email_domains, tenacity; print('ok')"
3. Run app:         python -m uvicorn app.main:app --reload --port 8000
   → http://localhost:8000
4. API docs:        http://localhost:8000/docs  (FastAPI auto-generated)
5. Tests:           python -m pytest tests/ -q  → 26 passing
6. Lint:            python -m ruff check app/ tests/  → 0 errors
7. Health check:    GET http://localhost:8000/api/health
   → {"status":"ok","providers_enabled":["local","bouncify"]}
8. Bouncify key:    In .env as BOUNCIFY_API_KEY (never commit)
9. DB file:         email_validator.db (auto-created on first run, git-ignored)
10. Schema change?  SQLite: delete email_validator.db. Neon: for missing COLUMNS,
    append the (table, column, DDL) tuple to `_PG_COLUMN_ADDS` in app/db.py — it
    runs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` at startup (idempotent).
    For new tables, create_all handles it automatically. Drops/renames still
    require manual SQL.
11. Auth:           Login at /login — email + password. New users start inactive (admin must activate).
12. Admin panel:    /admin — requires role 'admin' or 'superadmin'.
13. Superadmin:     Set SUPERADMIN_EMAIL in Vercel env — promoted on every startup (idempotent).
```

**Do NOT:**
- Use `pip install -e ".[dev]"` — editable install broken on Python 3.14 + setuptools. Install deps directly: `pip install fastapi uvicorn httpx pydantic pydantic-settings sqlmodel jinja2 python-multipart email-validator dnspython disposable-email-domains tenacity aiofiles pytest pytest-asyncio respx ruff bcrypt psycopg2-binary`
- Use `passlib[bcrypt]` — incompatible with bcrypt>=5.0 (`detect_wrap_bug` ValueError on startup). Use `bcrypt` directly: `bcrypt.hashpw()` + `bcrypt.checkpw()`.
- Use old Starlette TemplateResponse signature: `templates.TemplateResponse("page.html", {"request": request, ...})` — broken on Starlette 1.3.1. Always use: `templates.TemplateResponse(request, "page.html", {...})` (request first, no `request` in context dict)
- Cache `unknown` verdicts — they are transient (network/API errors). Only `valid`, `invalid`, `risky` go into `EmailCache`
- Commit `.env` — it contains `BOUNCIFY_API_KEY`. Pre-scan before every commit
- Rename or move `email_validator.db` manually — if schema changes, delete it (SQLModel auto-recreates tables on startup via `create_db_tables()`)
- Call `get_all_providers()` without a live `httpx.AsyncClient` — the lifespan hook in `main.py` sets `registry._client`. Outside lifespan (e.g. tests), the client is lazy-created but never closed properly
- Use `validate()` directly from routes — always use `validate_with_cache()` so cache is checked first
- Add a new SQLModel **column** without registering it in `_PG_COLUMN_ADDS` (app/db.py) — `create_all()` will silently skip it on existing Postgres tables and you'll get `UndefinedColumn` 500s in production
- Use `hatchling` as build-backend — broken on Python 3.14. Use `setuptools.build_meta`
- Use `socket` to check domain instead of `dnspython` — dnspython is already a dep and handles edge cases (NXDOMAIN vs timeout)
- Put `request` in the Jinja2 context dict when using Starlette 1.3.1 — it causes an unhashable dict key in the Jinja2 LRU cache and a `TypeError` at runtime

---

## Current State

### Providers

| Provider | Status | Auth | Single | Bulk | Notes |
|---|---|---|---|---|---|
| `local` | Always enabled | None | ✅ | ✅ | syntax + MX + disposable + role + free-provider |
| `bouncify` | Enabled (key in .env) | `BOUNCIFY_API_KEY` | ✅ | ✅ (job poll) | primary paid provider |
| `zerobounce` | Disabled (no key) | `ZEROBOUNCE_API_KEY` | ✅ | ✅ (CSV upload) | not wired in UI yet |
| `neverbounce` | Disabled (no key) | `NEVERBOUNCE_API_KEY` | ✅ | ✅ (job poll) | not wired in UI yet |
| `hunter` | Disabled (no key) | `HUNTER_API_KEY` | ✅ | asyncio.gather | no native bulk |

### Strategies

| Strategy | Behavior | Cost |
|---|---|---|
| `bouncify_only` | Single provider, returns immediately | 1 credit |
| `local_first` | Local check first; skip paid API only on hard `invalid` | 0–1 credits |
| `consensus` | All enabled providers in parallel, majority vote | N credits |
| `waterfall` | local → hunter → bouncify → zerobounce; stop at first confident verdict | 0–N credits |

### Cache

- Table: `EmailCache` — normalized `email` (unique index), `verdict`, `provider_data` (JSON), `validated_at`, `expires_at`
- TTL: 30 days (env: `CACHE_TTL_DAYS=30`)
- Key: `email.strip().lower()`
- Hits return immediately (0 API calls). UI shows `⚡ cached` badge with validated date and expiry
- `unknown` verdicts never cached (transient)
- Lazy expiry: stale row deleted on next access. Bulk purge via `purge_expired()`

### Test Suite (26 tests)

```
tests/providers/test_bouncify.py   — 4 tests  (respx mocks, deliverable/undeliverable/accept_all/no_key)
tests/providers/test_local.py      — 7 tests  (syntax, MX, disposable, role, free, bulk)
tests/test_cache.py                — 7 tests  (miss/hit, case, expiry, upsert, unknown, purge)
tests/test_routes.py               — 8 tests  (health, JSON verify, cache hit, HTMX verify, cached badge, index+auth, jobs+auth, redirect-without-auth)
```

All external HTTP calls are mocked with `respx`. Auth tests use `auth_client` fixture (creates test admin, logs in via TestClient). No real API calls in tests.

---

## What's Built

### Entry Points

| Command | What |
|---|---|
| `python -m uvicorn app.main:app --reload` | Dev server, port 8000 |
| `python -m uvicorn app.main:app --workers 1` | Production (single worker, shares httpx client) |
| `python -m pytest tests/ -q` | 25 tests |
| `python -m ruff check app/ tests/` | Lint |

### Routes

| Method | Path | What |
|---|---|---|
| `GET` | `/` | Main UI — single email + bulk CSV tabs |
| `GET` | `/jobs` | Job history list (last 50) |
| `GET` | `/jobs/{id}` | Job detail + live progress (HTMX polls `/jobs/{id}/status`) |
| `GET` | `/jobs/{id}/status` | HTMX partial — progress bar |
| `POST` | `/api/verify` | JSON single-email verify → `SingleVerifyResponse` |
| `POST` | `/verify/htmx` | Form-encoded single verify → HTML partial (used by UI) |
| `POST` | `/api/bulk` | Upload CSV → queue job → `BulkJobResponse` |
| `GET` | `/api/bulk/{id}` | Poll job status → `BulkStatusResponse` |
| `GET` | `/api/bulk/{id}/download` | Download results CSV |
| `GET` | `/api/health` | Health + enabled providers |
| `GET` | `/docs` | FastAPI auto-generated OpenAPI docs |

### File Map

```
app/
  main.py               FastAPI app + lifespan (httpx client init/close)
  config.py             pydantic-settings — loads .env
  db.py                 SQLModel engine + create_db_tables()
  models.py             Job, EmailResult, EmailCache, ApiUsage
  schemas.py            ProviderResult, SingleVerifyRequest/Response, Bulk*

  providers/
    base.py             Provider Protocol
    local.py            LocalProvider — syntax + MX + disposable + role + SMTP
    bouncify.py         BouncifyProvider — single + bulk (job poll)
    zerobounce.py       ZeroBounceProvider
    neverbounce.py      NeverBounceProvider
    hunter.py           HunterProvider
    registry.py         get_all_providers(), get_enabled_providers()

  core/
    validator.py        validate() + validate_with_cache() — strategy dispatch
    cache.py            get_cached(), set_cache(), parse_cached_providers(), purge_expired()
    csv_io.py           parse_csv_emails(), write_results_csv()

  routes/
    health.py           GET /api/health
    api_single.py       POST /api/verify + POST /verify/htmx
    api_bulk.py         POST /api/bulk + GET /api/bulk/{id} + GET /api/bulk/{id}/download
    ui.py               GET / + /jobs + /jobs/{id} + /jobs/{id}/status

  workers/
    bulk_worker.py      process_bulk_job() — chunks, cache-aware, BackgroundTasks

  templates/
    base.html           Nav + Tailwind CDN + HTMX
    index.html          Single email form + bulk CSV upload (tab toggle)
    jobs.html           Job history table
    job.html            Job detail + live HTMX progress
    partials/
      job_progress.html   HTMX polling partial
      single_result.html  Verdict card with ⚡ cached badge

static/               (empty — Tailwind via CDN, HTMX via CDN)
tests/
uploads/              gitignored — CSV uploads + results_*.csv
```

### DB Tables

| Table | Purpose | Key Fields |
|---|---|---|
| `Job` | Bulk job tracking | `id`, `status`, `total`, `processed`, `strategy`, `providers`, `user_id` |
| `EmailResult` | Per-email result in a bulk job | `job_id`, `email`, `verdict`, `provider_data` (JSON) |
| `EmailCache` | 30-day result cache | `email` (unique), `verdict`, `provider_data`, `validated_at`, `expires_at` |
| `ApiUsage` | Per-provider daily call counter | `provider`, `date`, `calls` |
| `User` | Auth user | `id`, `email`, `password_hash`, `role`, `is_active`, `created_at`, `last_login`, `validation_limit` |
| `UserSession` | Session tokens | `id`, `user_id`, `token_hash` (SHA-256), `expires_at` — 7-day sliding TTL |
| `Team` | Org teams | `id`, `name`, `description`, `created_by` |
| `TeamMembership` | User↔Team join | `team_id`, `user_id`, `status` (pending/active/rejected), `approved_by` |
| `UserInvite` | One-time invite tokens | `email`, `token_hash` (SHA-256), `role`, `invited_by`, `expires_at`, `used_at` |
| `AuditLog` | Admin action history | `action`, `actor_id`, `actor_email`, `target_type`, `target_id`, `details`, `created_at` |
| `SystemSetting` | Platform-wide config | `key` (PK), `value`, `updated_at` — keys: registration_open, maintenance_mode, default_validation_limit |

---

## Critical Gotchas

### 1. Starlette 1.3.1 TemplateResponse API — ALWAYS request-first
Starlette ≥ 0.41 changed the signature. Old code `TemplateResponse("page.html", {"request": request})` causes `TypeError: cannot use 'tuple' as dict key (unhashable type: 'dict')` in the Jinja2 LRU cache on Python 3.14 — it silently passes `context` as `name`.

**Always use:**
```python
# ✅ Correct (Starlette 1.3.1)
templates.TemplateResponse(request, "page.html", {"key": "value"})

# ❌ Broken on Starlette 1.3.1
templates.TemplateResponse("page.html", {"request": request, "key": "value"})
```

### 2. SQLModel schema changes — column adds are auto-migrated, but only if registered
`create_db_tables()` calls `SQLModel.metadata.create_all(engine)` which creates missing **tables** but never alters existing ones. The lifespan also runs `_apply_lightweight_migrations()` (Postgres-only) which iterates `_PG_COLUMN_ADDS` in `app/db.py` and runs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

**When adding a column to a model:**
1. Add it to the SQLModel class
2. Append `('"tablename"', "column_name", "POSTGRES_TYPE")` to `_PG_COLUMN_ADDS` in `app/db.py`
3. Done — next cold start applies it idempotently

If you skip step 2, you'll see `psycopg2.errors.UndefinedColumn` 500s on any route that selects from that table. Drops, renames, and constraint changes still require manual SQL or Alembic.

**Local SQLite:** delete `email_validator.db` after any model change — `create_all` recreates everything fresh.

### 3. `unknown` verdict must never be cached
Transient errors (API timeout, rate limit, network failure) all return `verdict="unknown"`. If cached, a permanently `unknown` result means the email is never re-validated. `set_cache()` and `_validate_with_cache()` in bulk_worker both check `if verdict != "unknown"` before writing.

### 4. `validate_with_cache()` not `validate()` in routes
`app/routes/api_single.py` and `app/workers/bulk_worker.py` call `validate_with_cache()`. If you bypass it and call `validate()` directly, cache is never checked and API credits are burned on repeat queries.

### 5. disposable_email_domains uses `.blocklist`, not `.domains`
```python
# ✅ Correct
import disposable_email_domains
_DISPOSABLE = set(disposable_email_domains.blocklist)

# ❌ AttributeError
_DISPOSABLE = set(disposable_email_domains.domains)
```

### 6. SMTP probe off by default — port 25 blocked on most ISPs
`ENABLE_SMTP_PROBE=false` in .env. Never enable it in cloud deploy without verifying port 25 is open. When enabled, SMTP probe results are cached by `_CATCH_ALL_CACHE` in `local.py` to avoid re-probing catch-all domains.

### 7. Bouncify bulk fallback path
If Bouncify bulk job creation returns no `job_id` (API shape change or plan restriction), `bouncify.py:verify_bulk()` silently falls back to individual `verify()` calls via `asyncio.gather`. This is correct but means bulk pricing may apply differently. Watch the raw response in logs.

### 8. Single uvicorn worker only for production
`httpx.AsyncClient` is shared via `registry._client` (set in FastAPI lifespan). Multiple workers each get their own client and their own in-process state. For production: `--workers 1 --threads 8` until Redis + proper distributed state is added.

### 9. Respx must be used in tests — never hit real APIs
All provider tests use `respx.mock`. Any test that calls `httpx.AsyncClient.get/post` without a `respx.mock` context will make a real HTTP request (or fail with a connection error in CI). `conftest.py` uses in-memory SQLite via `StaticPool`.

### 10. EmailCache unique index + upsert pattern
`EmailCache.email` has `unique=True`. The upsert in `set_cache()` queries for an existing row first, then updates or inserts. If two coroutines race on the same email, you may get an `IntegrityError: UNIQUE constraint failed`. Current risk is low (single worker), but be aware.

---

## Common Issues & Fixes

### 1. "TemplateResponse TypeError unhashable dict key"
**Symptom:** `TypeError: cannot use 'tuple' as a dict key (unhashable type: 'dict')` on any template route.
**Cause:** Old-style `TemplateResponse("name.html", {"request": request, ...})` — context dict passed as `name` to Jinja2.
**Fix:** `templates.TemplateResponse(request, "name.html", {"key": "value"})` — request first, no `request` in dict.

### 2. "no such column: emailcache.expires_at"
**Symptom:** `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such column`.
**Cause:** Model was updated (EmailCache added) but old `email_validator.db` still exists with old schema.
**Fix:** `Remove-Item email_validator.db` then restart uvicorn. All tables are recreated automatically.

### 3. "pip install -e . fails on Python 3.14"
**Symptom:** `hatchling.build has no attribute prepare_metadata_for_build_editable` or `BackendUnavailable: Cannot import 'setuptools.backends.legacy'`.
**Cause:** Python 3.14 + editable install incompatibility with both hatchling and older setuptools.
**Fix:** Install deps directly without editable flag (see 60-Second Resume step 2).

### 4. "Tests fail: AttributeError on mock patch path"
**Symptom:** `AttributeError: module 'app.routes.api_single' does not have the attribute 'validate'`.
**Cause:** Route uses `validate_with_cache` but test patches `validate`. Must patch the name as imported in the module.
**Fix:** `patch("app.routes.api_single.validate_with_cache", ...)` — patch at the import site, not the definition site.

### 5. "Bulk job stuck at 'queued'"
**Symptom:** Job status never moves past `queued` even after upload.
**Cause:** `BackgroundTasks` only runs after the response is sent. If the TestClient or request lifecycle ends before the background task starts, the task is abandoned.
**Fix:** In production this works fine. In tests, call `process_bulk_job()` directly (await it), don't rely on BackgroundTasks.

### 6. "Bouncify returns unknown for all emails in tests"
**Symptom:** All test emails return `status="unknown"`.
**Cause:** Test forgot to mock the Bouncify endpoint with `respx`. Real HTTP call either fails (no network in CI) or hits real API (burns credits).
**Fix:** Wrap test in `with respx.mock:` and add `respx.get("https://api.bouncify.io/v1/verify").mock(...)`.

### 7. "Cache miss on email that was just validated"
**Symptom:** Second call still hits API instead of cache.
**Cause 1:** Email casing differs — `User@Gmail.com` vs `user@gmail.com`. Cache key is always `.lower()`.
**Cause 2:** Verdict was `unknown` — not cached by design.
**Cause 3:** DB was deleted between calls (dev workflow).

### 8. "disposable_email_domains import fails"
**Symptom:** `AttributeError: module 'disposable_email_domains' has no attribute 'blocklist'`.
**Cause:** Package version < 0.0.87 uses `.domains` attribute.
**Fix:** `pip install --upgrade disposable-email-domains` (note: pip name has hyphens, import uses underscores).

---

## Open Items Backlog

### Phase 1 — Quick Wins (approved, next sprint)

| ID | Item | Notes |
|---|---|---|
| P1-1 | **Dashboard home** — stats widget: total validated, cache hit %, daily API usage, last 5 jobs | New route `GET /` replaces current tab layout |
| P1-2 | **Cache browser** — `/cache` page: searchable table, filter by verdict/expiry, one-click re-validate | New route + HTMX search |
| P1-3 | **Smart CSV export** — filter by verdict (valid/invalid/risky only), choose provider columns | Update `csv_io.py` + download route |

### Phase 2 — Core Upgrades (approved, next sprint)

| ID | Item | Notes |
|---|---|---|
| P2-1 | **Analytics page** — verdict trends, provider agreement rate, top invalid domains, cache hit rate | Chart.js via CDN |
| P2-2 | **API key management UI** — settings page to enter/test/mask keys, show balance if provider returns it | New `GET/POST /settings` route |
| P2-3 | **Bulk progress v2** — per-provider live breakdown, cache hit counter, ETA, pause/resume | Upgrade bulk_worker + job.html |
| P2-4 | **Webhook notify** — POST to URL when bulk job completes | New `webhook_url` field on Job model |
| P2-5 | **Domain reputation** — `GET /api/domain/{domain}` aggregates cached results for a domain | New route, query EmailCache |
| P2-6 | **Dark mode** — Tailwind `dark:` classes + localStorage toggle | Update base.html + all templates |

### Phase 3 — UI Redesign (approved)

| ID | Item | Notes |
|---|---|---|
| P3-1 | Left sidebar nav (Dashboard / Validate / Cache / Analytics / Settings) | Replace top nav bar |
| P3-2 | Result cards with confidence score bar + copy button | Update single_result.html |
| P3-3 | Provider status dots in sidebar (live/rate-limited/unconfigured) | Wire to `/api/health` |
| P3-4 | Strategy selector as visual cards (cost vs accuracy) | Replace plain `<select>` |
| P3-5 | Drag-to-upload zone with column preview | Update bulk upload tab |

### Phase 4 — Future (not started)

| ID | Item |
|---|---|
| P4-1 | Zapier / n8n integration — expose `/api/verify` as Zapier action |
| P4-2 | Multi-user / auth — FastAPI-Users + Google OAuth |
| P4-3 | Scheduled re-validation — cron job for saved lists, email diff report |
| P4-4 | JS + Python SDK — thin wrappers, publish to npm + PyPI |
| P4-5 | AI triage — Claude Haiku for `risky` verdict scoring |
| P4-6 | Postgres + Redis — replace SQLite/in-process cache for multi-worker deploy |

---

## Session History

| Session | Date | Version | Key Work |
|---|---|---|---|
| 1 | 2026-06-23 | v0.1.0 | Initial build — FastAPI scaffold, 5 providers, 4 strategies, SQLite+SQLModel, HTMX+Tailwind UI, bulk CSV pipeline, BackgroundTasks worker, Jinja2 templates, 16 tests passing |
| 2 | 2026-06-24 | v0.2.0 | Email result cache — `EmailCache` table, 30-day TTL, `validate_with_cache()`, cache-aware bulk worker, `⚡ cached` badge, `purge_expired()`, 7 new cache tests → 25 total. PROJECT_LOG created. |
| 3 | 2026-06-24 | v0.3.0 | Phase 1+2+3 — sidebar layout + dark mode, Dashboard, Validate (strategy cards + drag-drop), Cache Browser (HTMX), Analytics (Chart.js), Settings, domain lookup, smart CSV export, confidence score cards. 25 tests, ruff clean. |
| 4 | 2026-06-24 | v0.4.0 | Top navbar refactor (replaced sidebar) + Neon PostgreSQL + GitHub Actions bulk flow. Deployed to Vercel. |
| 5 | 2026-06-24 | v0.5.0 | Session-based auth — login/register/logout, `User`+`UserSession`+`Team`+`TeamMembership` tables, three-tier roles (user/admin/superadmin), `SUPERADMIN_EMAIL` env bootstrap, admin panel (`/admin`) with dark indigo sidebar, users/teams/stats/usage/providers pages, split-panel login design, avatar dropdown in nav, 39-check pre-push checklist, 26 tests. `bcrypt` direct (passlib dropped). |
| 6 | 2026-06-24 | v0.6.0 | Hotfixes — missing `user_id` column on `job` table (ALTER TABLE on Neon), `RedirectResponse` import in ui.py, `UTC` import cleanup, E501 line-length fixes, admin/superadmin nav visibility fix (role check was `=='admin'` not `in ('admin','superadmin')`), mobile menu Teams+Admin links, avatar dropdown role badge + Admin panel quick-link. |
| 6b | 2026-06-24 | v0.6.1 | User invite flow — `UserInvite` model, `POST /admin/invite`, `POST /admin/invites/{id}/revoke`, `GET/POST /invite/{token}`, invite.html, users.html invite modal + URL banner + pending invites table. SHA-256 token pattern, superadmin-only admin invites, auto-login on acceptance. |
| 7 | 2026-06-24 | v0.7.0 | Admin features A2→A6 + design overhaul D1-D7 — A2: user search/filter by email/role/status; A1: AuditLog model + log all write actions, `/admin/audit-log` with pagination; A3: `/admin/sessions` session manager (superadmin, revoke any session); A4: SystemSetting model, `/admin/sys-settings` (registration_open, maintenance_mode, default_validation_limit); A5: User.validation_limit monthly cap enforced in HTMX verify, progress bar in users table, set-limit modal; A6: dashboard quick-action cards + superadmin section + dark-mode-aware chart; D1-D7: admin sidebar sectioned (Data/Access/Config/Superadmin), dark mode toggle in admin, maintenance mode 503 handler, register.html already matched login design. Neon migration: auditlog + systemsetting tables created, validation_limit column added. |
| 8 | 2026-06-24 | v0.8.0 | **Vercel runtime fix + auto-migrations + navbar redesign**. Dropped Mangum (returns AWS Lambda response shape → Vercel rejects with `FUNCTION_INVOCATION_FAILED`); `api/index.py` now exposes ASGI `app` directly and Vercel auto-detects it. Added lifespan schema migration `_apply_lightweight_migrations()` in `app/db.py` that runs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` from `_PG_COLUMN_ADDS` — fixes the `user.validation_limit` missing-column 500 on Neon and prevents class of bug going forward (just append to the list when adding a column). Navbar redesign D8: replaced emoji icons with inline Lucide SVGs, subtle underline active state instead of indigo pill, backdrop-blur translucent header, gradient brand mark with indigo glow on hover, provider-dots in a small pill container, avatar uses gradient + hover ring, theme toggle uses sun/moon SVGs that swap via `dark:` (no JS textContent hack). Removed `mangum` from requirements. Pre-push checklist updated (38 checks; no longer asserts Mangum presence). |
| 9 | 2026-06-27 | v0.9.0 | **SMTP transactional email + team ownership + ops hardening for free-tier infra**. New `app/services/email.py` mailer (stdlib `smtplib`, async via `to_thread`, Gmail-friendly STARTTLS/465). Four templated emails wired with failure-isolated try/except: invite link, admin-notify on self-registration, user-notify on activate, password reset (30-min TTL via new `PasswordReset` model + `/forgot-password` + `/reset-password/{token}` flow, account-enumeration-safe). Profile page (`/profile`) with change-email / change-password (current-pw confirmation, collision check) + "sign out other devices". Auth lockout: 5 failed logins → 15-min `locked_until` on `User` (returns 429). Team ownership: `role` column on `TeamMembership` ("owner"/"member"), creator auto-added as owner on create, "Make owner" button to transfer ownership, owner-removal blocked, team edit modal, audit entries `team.create`/`team.edit`/`team.transfer_ownership`. Startup `backfill_team_owners()` so legacy teams get an owner row. Bulk uploads now accept `.xlsx`/`.xlsm` (openpyxl converts to CSV server-side), paste-emails sub-tab in `/validate` (client builds `pasted.csv` blob), downloadable CSV+XLSX templates at `/api/bulk/template.{csv,xlsx}`, CSV export for cache browser (`/api/cache/export`) and audit log (`/admin/audit-log/export`, self-audited). Vercel deploy fixes after dispatch experiments: `_trigger_github_actions` runs INLINE (Vercel kills BackgroundTasks the moment the response is sent — pre-fix jobs sat queued forever); httpx timeout 8→4s; in-process fallback gated on `not os.getenv("VERCEL")`. Cold-start hardening: `_safe_startup()` wraps every lifespan DB op with a 4s `asyncio.to_thread` ceiling so a cold Neon never blocks app readiness; dashboard `/` aggregates moved off the request thread, bounded at 6s, plus a 30s in-process cache to skip repeat COUNT(*) on the same warm function. Empty-string env vars now drop to defaults via a `@model_validator(mode="before")` (a blank `CACHE_TTL_DAYS=""` repo variable was crashing the GitHub Actions worker before any code ran). Patched `/api/health` to `SELECT 1` so an external pinger actually wakes Neon. New `.github/workflows/keep_warm.yml` cron every 3 min (offset off the hour grid to dodge GitHub's scheduler congestion) hitting `${{ vars.APP_URL }}/api/health`. README gains status badges for both workflows. Fixed `GITHUB_REPO` default from the prior owner's name to `Surya8991/Email-Validator`. Forgot-password / change-password mail paths require `bcrypt` directly (same pattern as login). Dependencies: `openpyxl>=3.1.0` for XLSX import/export. New models: `PasswordReset`; new columns: `user.failed_login_count`, `user.locked_until`, `teammembership.role` (all in `_PG_COLUMN_ADDS`). |

---

## Env Vars Quick Reference

| Var | Required | Default | Purpose |
|---|---|---|---|
| `BOUNCIFY_API_KEY` | For Bouncify | `""` | Primary paid provider |
| `ZEROBOUNCE_API_KEY` | For ZeroBounce | `""` | Auto-disabled if empty |
| `NEVERBOUNCE_API_KEY` | For NeverBounce | `""` | Auto-disabled if empty |
| `HUNTER_API_KEY` | For Hunter.io | `""` | Auto-disabled if empty |
| `ENABLE_SMTP_PROBE` | No | `false` | SMTP RCPT probe — off by default (port 25 blocked on most hosts) |
| `SMTP_PROBE_FROM` | If SMTP enabled | `probe@example.com` | MAIL FROM address for SMTP probe |
| `CACHE_TTL_DAYS` | No | `30` | Email result cache lifetime in days |
| `BOUNCIFY_DAILY_CAP` | No | `500` | Max daily Bouncify calls (0 = unlimited) |
| `ZEROBOUNCE_DAILY_CAP` | No | `0` | Max daily ZeroBounce calls |
| `NEVERBOUNCE_DAILY_CAP` | No | `0` | Max daily NeverBounce calls |
| `HUNTER_DAILY_CAP` | No | `0` | Max daily Hunter.io calls |
| `APP_HOST` | No | `0.0.0.0` | uvicorn bind host |
| `APP_PORT` | No | `8000` | uvicorn bind port |
| `LOG_LEVEL` | No | `info` | uvicorn log level |
| `DATABASE_URL` | Production | `""` | Neon/Supabase Postgres URL (any `postgres://` or `postgresql://` auto-normalized to `+psycopg2`) |
| `SECRET_KEY` | Production | dev value | Random hex for session signing — `openssl rand -hex 32` |
| `PRODUCTION` | No | `false` | Marks deploy as prod (stricter cookie flags etc.) |
| `MAX_BULK_EMAILS` | No | `0` | Hard cap on bulk-upload rows (0 = unlimited) |
| `HTTPX_TIMEOUT` | No | `10.0` | httpx timeout — keep ≤ 8 on Vercel Hobby |
| `GITHUB_PAT` | For bulk on Vercel | `""` | Fine-grained PAT, Actions: read/write — triggers `bulk_process.yml` |
| `GITHUB_REPO` | No | `Surya8991/Email-Validator` | `owner/repo` for workflow_dispatch |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Bootstrap | `""` | First admin if `User` table is empty |
| `SUPERADMIN_EMAIL` | Bootstrap | `""` | Promoted to superadmin on every startup (idempotent) |
| `SMTP_HOST` | For email | `""` | Leave blank to disable all outbound mail |
| `SMTP_PORT` | No | `587` | 587=STARTTLS, 465=SSL (auto-switches) |
| `SMTP_USER` / `SMTP_PASSWORD` | If SMTP | `""` | For Gmail: account + App Password (NOT regular password) |
| `SMTP_USE_TLS` | No | `true` | STARTTLS on 587; ignored for 465 |
| `SMTP_FROM` | No | `SMTP_USER` | Must equal `SMTP_USER` for Gmail |
| `SMTP_FROM_NAME` | No | `Email Validator` | Display name in the From header |
| `SMTP_TIMEOUT` | No | `15.0` | Per-connection SMTP timeout |

### GitHub repo Variables (Settings → Secrets and variables → Actions → Variables)
| Var | Used by | Value |
|---|---|---|
| `APP_URL` | `keep_warm.yml` | Deployed origin, no trailing slash, e.g. `https://email-validator-lilac.vercel.app` |
| `CACHE_TTL_DAYS` | `bulk_process.yml` | e.g. `30`. Empty values are now tolerated thanks to the `_drop_empty_env_values` model validator in `app/config.py`. |

### GitHub repo Secrets
| Secret | Used by | Notes |
|---|---|---|
| `DATABASE_URL` | `bulk_process.yml` | Must match the Vercel app's DB — otherwise the worker can't see jobs the app created. |
| `BOUNCIFY_API_KEY` | `bulk_process.yml` | Same as Vercel. |
| `ZEROBOUNCE_API_KEY` / `NEVERBOUNCE_API_KEY` / `HUNTER_API_KEY` | `bulk_process.yml` | Optional, only if those providers are enabled. |

---

## Free-Tier Infra Notes (read before debugging timeouts)

The app is deployed on **Vercel Hobby (10s function timeout)** with **Neon Free (5-min idle auto-pause)**. The two together create a cold-start chain that has caused most production incidents:

1. No traffic for 5 min → Neon pauses.
2. Next request hits Vercel → Vercel cold-starts the function.
3. Function tries to query → Neon is still resuming (5-8s) → 10s budget burned → 504.

Mitigations now in code:
- **`keep_warm.yml`** GitHub Actions cron every 3 min pings `/api/health` (which runs a `SELECT 1` against the DB). Schedule offset to 1,4,7,... to dodge GitHub's hour-aligned scheduler congestion.
- **`_safe_startup()`** in `app/main.py` bounds every lifespan DB op at 4s via `asyncio.wait_for(asyncio.to_thread(...))` — partial failures print and continue; the operations are all idempotent so the next request that needs them retries naturally.
- **Dashboard cache** — `/` aggregates run via `asyncio.to_thread` with a 6s ceiling and cache for 30s. Without this, 3 sequential `COUNT(*)` queries on Neon free tier reliably 504'd the dashboard.
- **`/api/bulk` dispatches inline** — Vercel kills FastAPI BackgroundTasks the instant a response is sent, so the GitHub dispatch MUST run before the response. httpx timeout is 4s. In-process fallback is gated on `not os.getenv("VERCEL")` because it can't survive there anyway.

Setup checklist for a healthy free-tier deploy:
- [ ] `APP_URL` repo variable set (otherwise keep-warm exits with "not set")
- [ ] `GITHUB_PAT` Vercel env var set (otherwise bulk jobs queue but never dispatch)
- [ ] `DATABASE_URL` set both in Vercel **and** in GitHub repo secrets (must be the same DB)
- [ ] First Keep Warm run triggered manually after setup (auto-cron can take 30-60 min to start firing the first time on a new repo)

If still seeing 504s, the order of triage:
1. `GET /api/health` returns JSON? If yes, Vercel+DB are healthy; the problem is a specific slow route.
2. Vercel logs for the offending route — look for slow queries or import-time failures.
3. Neon dashboard — is the compute green/active?

---

## Roadmap

### Next (Phase 1 + Phase 2 + UI redesign) — approved
Dashboard home → Cache browser → Smart export → Analytics → Settings UI → Bulk v2 → Webhook → Domain reputation → Dark mode → Full sidebar layout + result card redesign

### Phase 4 (future)
Zapier / n8n → Multi-user auth → Scheduled re-validation → SDK → AI triage (Haiku) → Postgres + Redis

---

_Last updated: 2026-06-27 — Session 9 — v0.9.0_
