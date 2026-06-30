# Email Validator

[![Keep Warm](https://github.com/Surya8991/Email-Validator/actions/workflows/keep_warm.yml/badge.svg)](https://github.com/Surya8991/Email-Validator/actions/workflows/keep_warm.yml)
[![Bulk Email Validation](https://github.com/Surya8991/Email-Validator/actions/workflows/bulk_process.yml/badge.svg)](https://github.com/Surya8991/Email-Validator/actions/workflows/bulk_process.yml)
[![CI](https://github.com/Surya8991/Email-Validator/actions/workflows/ci.yml/badge.svg)](https://github.com/Surya8991/Email-Validator/actions/workflows/ci.yml)

Multi-provider email validator (Bouncify + free local stack) with auth, bulk CSV/XLSX processing, caching, and an admin panel. FastAPI on Vercel + Neon Postgres + GitHub Actions for long-running bulk jobs.

Current version: **0.14** — Filtered CSV exports on every list page (`/jobs`, `/admin/users`, `/admin/usage`); existing `/cache` + `/admin/audit-log` exports already honored filters. Prior in **0.13**: filters & pagination across high-traffic pages + audit-log count bug fix. See [PROJECT_LOG.md](PROJECT_LOG.md) Session 21.

---

## Quick Start (local)

```bash
git clone https://github.com/Surya8991/Email-Validator.git
cd Email-Validator
pip install -r requirements.txt

# Minimum .env
cat > .env <<EOF
BOUNCIFY_API_KEY=...
SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
ADMIN_EMAIL=you@example.com
ADMIN_PASSWORD=changeme
EOF

python -m uvicorn app.main:app --reload
# http://localhost:8000  (redirects to /login)
```

---

## Deploy to Vercel

1. `vercel --prod`
2. Set these env vars on Vercel:

   | Required | Notes |
   |---|---|
   | `BOUNCIFY_API_KEY` | provider key |
   | `DATABASE_URL` | Neon `postgres://…` URL |
   | `SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
   | `ADMIN_EMAIL`, `ADMIN_PASSWORD` | bootstrap on first run |
   | `SUPERADMIN_EMAIL` | promoted on every startup |
   | `GITHUB_PAT` | fine-grained PAT — repo: `Email-Validator`, **Actions: Read+Write** |
   | `GITHUB_REPO` | `owner/repo` of this repo |
   | `JOB_CALLBACK_TOKEN` | shared secret the `bulk_process` workflow uses to call `/api/bulk/{id}/workflow-callback` on success / failure / cancel. Must match the GitHub repo secret of the same name. Generate: `python -c "import secrets; print(secrets.token_hex(16))"`. Without it, jobs cancelled in the GitHub UI stay `running` forever. |
   | `BASE_URL` | public origin (e.g. `https://validator.example.com`) — used for outbound reset/invite links; must be set in production |
   | `PRODUCTION` | `true` to enable HSTS + `secure` session cookies |

   Optional tuning (sensible defaults shipped):

   | Env | Default | What |
   |---|---|---|
   | `MAX_BULK_EMAILS` | `1000` | Per-CSV upload cap. 400 if exceeded. `0` disables. |
   | `MAX_USER_ACTIVE_JOBS` | `4` | Per-user queued+running jobs cap. 429 if exceeded. |
   | `MAX_USER_ACTIVE_EMAILS` | `2000` | Per-user sum-of-pending-emails cap. 429 if exceeded. |
   | `UNKNOWN_STRIKES` | `3` | After this many failed retries, `EmailResult.verdict` flips from `unknown` to `invalid` (see Retry sweep below). |

3. Set these **GitHub Actions secrets** so the bulk + retry workers hit the same DB/provider as Vercel: `DATABASE_URL`, `BOUNCIFY_API_KEY`, `JOB_CALLBACK_TOKEN`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `SUPERADMIN_EMAIL`.

   > `ADMIN_EMAIL` / `ADMIN_PASSWORD` / `SUPERADMIN_EMAIL` are now read by `db_init.yml` (runs on every push to main) instead of Vercel's lifespan. Remove them from Vercel env vars if you had them there — they're no longer needed at runtime.

4. Set these **GitHub Actions variables** (Settings → Secrets and variables → Actions → Variables tab) — all optional:

   | Var | Effect |
   |---|---|
   | `APP_URL` | If set (e.g. `https://your-app.vercel.app`), the bulk_process workflow's final `if: always()` step POSTs the run conclusion to `/api/bulk/{id}/workflow-callback`. Without it, the notify step is a silent no-op. |
   | `CHUNK_SIZE` | Per-email path: in-flight concurrency per `asyncio.gather`. Defaults: 20 in bulk_process, 5 in retry_unknowns. |
   | `BULK_SUB_BATCH` | Bulk-path: emails per Bouncify bulk submission. Default 500. |
   | `BOUNCIFY_BULK` | Set `1` to enable the bulk-API path for `bouncify_only` / `local_first` jobs (~10× faster on ≥1k rows). Default off pending a confidence-building comparison run. |
   | `CACHE_TTL_DAYS` | Cache lifetime default override. |

5. Bulk jobs auto-dispatch from `/api/bulk` → GitHub Actions runs `bulk_process.yml` → writes back to Neon → UI polls progress. Workflow's final step calls back into the app on success / failure / cancel so cancelled-in-the-GH-UI runs no longer stay `running` forever.

6. **DB init runs via GitHub Actions, not Vercel.** Every push to `main` triggers `db_init.yml` which runs `create_db_tables`, admin bootstrap, and team-owner backfill against Neon directly. Vercel cold starts no longer do any DB ops — this keeps them under the 10s Hobby limit even on the first request after a Neon idle-pause.

6. The `bulk_process` and `retry_unknowns` workflows are both capped at **10 concurrent runs** via a 10-bucket `concurrency:` group keyed on the last digit of `job_id` / `bucket`. An 11th dispatch waits in GitHub's own queue until a slot frees up. Bouncify rate limits may push the `unknown` count up at this concurrency on lower tiers — re-resolve those via the retry-unknowns sweep.

---

## Validation strategies

| Strategy | Cost | What it does |
|---|---|---|
| `bouncify_only` (default) | 0–1 credit/email | Free local syntax+MX pre-filter; Bouncify only for the rest |
| `local_first` | 0–1 credit/email | Local first, paid API only when local isn't conclusive |
| `consensus` | $$$ | All providers in parallel, majority vote |
| `waterfall` | $$ | Cascade, stop at first confident verdict |

Cache hits short-circuit before any provider is called (TTL configurable per request, default 30d).

---

## API

Full OpenAPI docs at `/docs`. **All `/api/*` endpoints require a session cookie via `/login`** — anonymous access returns 401/redirect.

```
POST   /api/verify                              single email
POST   /api/bulk                                upload CSV/XLSX → job_id
GET    /api/bulk/{id}                           poll status
GET    /api/bulk/{id}/download                  download results CSV
POST   /api/bulk/{id}/retry                     owner/admin — re-dispatch a failed job
POST   /api/bulk/{id}/workflow-callback         called by GH Actions on run conclusion
DELETE /api/bulk/{id}                           admin — delete a job + its results
POST   /api/bulk/clear                          admin — delete all non-running jobs

GET    /api/cache/export                        export full cache to CSV
DELETE /api/cache/{id}                          delete one cache row
POST   /api/cache/purge                         delete expired rows
POST   /api/cache/clear                         admin — delete every cache row

POST   /admin/retry-unknowns                    admin — dispatch retry_unknowns workflow
POST   /admin/cache-lookup                      admin — bulk cache verdict lookup (Account Cleanup)

GET    /api/stats                               verdict + cache aggregates
GET    /api/domain/{domain}                     domain reputation summary
GET    /api/health                              status + db_ok + enabled providers
```

## Retry sweep for persistent unknowns

`scripts/retry_unknowns.py` (+ `.github/workflows/retry_unknowns.yml`) re-validates `EmailResult.verdict='unknown'` rows in 500-email batches.

Each pass increments `EmailResult.retry_count` (added by `_apply_lightweight_migrations` on Postgres startup). After `UNKNOWN_STRIKES` (default 3) failed re-validations, the row's verdict flips from `unknown` to `invalid` so it leaves the retry pool — persistent unknowns are dead-MX / parked domains in practice, treating them as invalid stops re-burning Bouncify credits forever.

Trigger from the UI: `/admin/stats` shows a "↻ Retry N unknowns" button when verdict_counts['unknown'] > 0 (capped at 10,000 emails per click).

Runs **automatically every day at 3 AM** via a schedule trigger in `retry_unknowns.yml`. You can still trigger manually:
```sh
gh workflow run retry_unknowns.yml \
  -f batch_size=500 -f max_batches=20 -f strikes=3
```

---

## Tests & lint

```bash
pytest -q                          # 36 tests, all external HTTP mocked
ruff check .
pip install pip-audit && pip-audit # CVE scan on all dependencies
bash scripts/pre_push_check.sh     # pre-push gate
```

CI runs all three (ruff, mypy, pytest, pip-audit) on every push and PR via `.github/workflows/ci.yml`. Dependabot opens weekly PRs for outdated pip packages and GitHub Actions versions.

---

## Notes

- All routes auth-gated. Sessions: SHA-256-hashed tokens in DB, HttpOnly cookie. New registrations start inactive — admin must approve. Password change/reset revokes every existing session.
- Single-verify is ownership-scoped. Bulk job DELETE is **admin-only** (was owner-or-admin until 0.11 — locked down so end-users can't wipe history). Job owner emails are visible to everyone on `/jobs`, `/jobs/{id}`, and the dashboard's Recent Bulk Jobs card.
- Per-IP rate limits on `/login`, `/forgot-password`, `/register`. Failed-login lockout still applies per-account.
- Origin-check + security-headers middleware (HSTS in prod) provide CSRF / clickjacking / sniffing defence on top of `samesite="lax"`.
- Timestamps render in **IST** (UTC+5:30) — DB stays naive-UTC, conversion is display-only via `app/templating.py`.
- Bulk jobs show a live **ETA** while running, plus a global HTMX progress bar for every in-flight request. Job list auto-refreshes while anything is queued or running.
- Free-tier safe: `keep_warm.yml` (×3 redundant workflows) pings `/api/health` every 5 min so Neon doesn't auto-pause. DB ops (`create_db_tables`, admin bootstrap, team backfill) run via `db_init.yml` on push rather than in the Vercel lifespan — cold starts now take ~2s, well within Hobby's 10s limit.
- **Stale job watchdog:** `stale_jobs.yml` runs hourly and marks any job stuck in `running` for >7h as `failed`, catching GitHub Actions runner kills that the workflow-callback missed.
- **List page filters & pagination** (added in 0.13):
  - `/jobs` — status (queued/running/done/failed) + owner-email filter (admin-only) + page-based pagination (50/page). Filter persists across the live 5s auto-refresh.
  - `/cache` — verdict (valid/invalid/risky) filter next to the existing email search. Both flow into the CSV export query so the download matches the on-screen view.
  - `/admin/audit-log` — actor-email + from/to date filters added on top of the existing action filter. Pagination preserves all four filters; total-count query was also fixed (previously ignored filters, making page math wrong).
- **Filtered CSV exports** (added in 0.14) — every list page now has an `⬇ Export CSV` button that streams the current filtered view. Endpoints: `/jobs/export` (status+owner), `/admin/users/export` (q+role+status), `/admin/usage/export` (full per-user activity + provider totals), in addition to the existing `/api/cache/export` (q+verdict) and `/admin/audit-log/export` (action+actor+dates). Exports of admin-only tables are audit-logged.
- **Read [PROJECT_LOG.md](PROJECT_LOG.md) before changing anything** — has the do-not-regress list, env-var table, and the Workflow Runbook for triaging Vercel + GHA failures.
