# AI Email Validator — Master Project Log

> **ACCOUNT-SWITCH PROOF. Read every section before touching any code.**
> Last updated: 2026-06-24 (Session 2). Current VERSION: **0.2.0**

---

## 60-Second Resume

```
1. cd "C:\Users\Surya L\Desktop\AI Agents\AI Email Vaildator"
2. Verify imports:  python -c "import fastapi, sqlmodel, httpx, email_validator, dns, disposable_email_domains, tenacity; print('ok')"
3. Run app:         python -m uvicorn app.main:app --reload --port 8000
   → http://localhost:8000
4. API docs:        http://localhost:8000/docs  (FastAPI auto-generated)
5. Tests:           python -m pytest tests/ -q  → 25 passing
6. Lint:            python -m ruff check app/ tests/  → 0 errors
7. Health check:    GET http://localhost:8000/api/health
   → {"status":"ok","providers_enabled":["local","bouncify"]}
8. Bouncify key:    In .env as BOUNCIFY_API_KEY (never commit)
9. DB file:         email_validator.db (auto-created on first run, git-ignored)
10. Schema change?  Delete email_validator.db before restarting — SQLModel
    does NOT run migrations, it only creates missing tables.
```

**Do NOT:**
- Use `pip install -e ".[dev]"` — editable install broken on Python 3.14 + setuptools. Install deps directly: `pip install fastapi uvicorn[standard] httpx pydantic pydantic-settings sqlmodel jinja2 python-multipart email-validator dnspython disposable-email-domains tenacity aiofiles pytest pytest-asyncio respx ruff`
- Use old Starlette TemplateResponse signature: `templates.TemplateResponse("page.html", {"request": request, ...})` — broken on Starlette 1.3.1. Always use: `templates.TemplateResponse(request, "page.html", {...})` (request first, no `request` in context dict)
- Cache `unknown` verdicts — they are transient (network/API errors). Only `valid`, `invalid`, `risky` go into `EmailCache`
- Commit `.env` — it contains `BOUNCIFY_API_KEY`. Pre-scan before every commit
- Rename or move `email_validator.db` manually — if schema changes, delete it (SQLModel auto-recreates tables on startup via `create_db_tables()`)
- Call `get_all_providers()` without a live `httpx.AsyncClient` — the lifespan hook in `main.py` sets `registry._client`. Outside lifespan (e.g. tests), the client is lazy-created but never closed properly
- Use `validate()` directly from routes — always use `validate_with_cache()` so cache is checked first
- Add new SQLModel table without deleting old DB first — `SQLModel.metadata.create_all()` only creates missing tables, does not run ALTER TABLE
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

### Test Suite (25 tests)

```
tests/providers/test_bouncify.py   — 4 tests  (respx mocks, deliverable/undeliverable/accept_all/no_key)
tests/providers/test_local.py      — 7 tests  (syntax, MX, disposable, role, free, bulk)
tests/test_cache.py                — 7 tests  (miss/hit, case, expiry, upsert, unknown, purge)
tests/test_routes.py               — 7 tests  (health, JSON verify, cache hit, HTMX verify, cached badge, index, jobs)
```

All external HTTP calls are mocked with `respx`. No real API calls in tests.

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
| `Job` | Bulk job tracking | `id`, `status` (queued/running/done/failed), `total`, `processed`, `strategy`, `providers` |
| `EmailResult` | Per-email result in a bulk job | `job_id`, `email`, `verdict`, `provider_data` (JSON) |
| `EmailCache` | 30-day result cache | `email` (unique), `verdict`, `provider_data`, `validated_at`, `expires_at` |
| `ApiUsage` | Per-provider daily call counter | `provider`, `date`, `calls` (not yet wired to routes) |

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

### 2. SQLModel schema changes require DB delete
`create_db_tables()` calls `SQLModel.metadata.create_all(engine)` — this only creates missing tables, never alters existing ones. Adding a column to a model and restarting will NOT update the table. The symptom is a silent `OperationalError: no such column` at runtime.

**Fix:** Delete `email_validator.db` before restarting after any model change. In production: use Alembic.

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
| 1 | 2026-06-23 | v0.1.0 | Initial build — FastAPI scaffold, 5 providers (local/bouncify/zerobounce/neverbounce/hunter), 4 strategies, SQLite+SQLModel, HTMX+Tailwind UI, bulk CSV pipeline, BackgroundTasks worker, Jinja2 templates, 16 tests passing, Bouncify API key live-tested |
| 2 | 2026-06-24 | v0.2.0 | Email result cache — `EmailCache` table, 30-day TTL, `validate_with_cache()`, cache-aware bulk worker, `⚡ cached` badge in UI, `purge_expired()`, 7 new cache tests → 25 total passing. Phase 1+2+3 plan approved. PROJECT_LOG created. |
| 3 | 2026-06-24 | v0.3.0 | Phase 1+2+3 complete — sidebar layout + dark mode (base.html), Dashboard (`/`), Validate (`/validate`) with visual strategy cards + drag-drop, Cache Browser (`/cache`) HTMX search/delete, Analytics (`/analytics`) Chart.js pie/line/bar, Settings (`/settings`) provider status + domain lookup, `GET /api/stats`, `GET /api/domain/{domain}`, `DELETE /api/cache/{id}`, `POST /api/cache/purge`, smart CSV export (verdict filter), confidence score bar + copy button on result cards. 25 tests, ruff clean. |

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

---

## Roadmap

### Next (Phase 1 + Phase 2 + UI redesign) — approved
Dashboard home → Cache browser → Smart export → Analytics → Settings UI → Bulk v2 → Webhook → Domain reputation → Dark mode → Full sidebar layout + result card redesign

### Phase 4 (future)
Zapier / n8n → Multi-user auth → Scheduled re-validation → SDK → AI triage (Haiku) → Postgres + Redis

---

_Last updated: 2026-06-24 — Session 2 — v0.2.0_
