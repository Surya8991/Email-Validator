# Email Validator

[![Keep Warm](https://github.com/Surya8991/Email-Validator/actions/workflows/keep_warm.yml/badge.svg)](https://github.com/Surya8991/Email-Validator/actions/workflows/keep_warm.yml)
[![Bulk Email Validation](https://github.com/Surya8991/Email-Validator/actions/workflows/bulk_process.yml/badge.svg)](https://github.com/Surya8991/Email-Validator/actions/workflows/bulk_process.yml)

Multi-provider email validator (Bouncify + free local stack) with auth, bulk CSV/XLSX processing, caching, and an admin panel. FastAPI on Vercel + Neon Postgres + GitHub Actions for long-running bulk jobs.

Current version: **0.10.1** (bulk throughput — Bouncify bulk API wired up. See [PROJECT_LOG.md](PROJECT_LOG.md) Session 14.)

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
   | `BASE_URL` | public origin (e.g. `https://validator.example.com`) — used for outbound reset/invite links; must be set in production |
   | `PRODUCTION` | `true` to enable HSTS + `secure` session cookies |

3. Set the same `DATABASE_URL` and `BOUNCIFY_API_KEY` as **GitHub Actions secrets** (Settings → Secrets → Actions) so the bulk worker hits the same DB and provider.

4. Bulk jobs auto-dispatch from `/api/bulk` → GitHub Actions runs `bulk_process.yml` → writes back to Neon → UI polls progress.

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
POST   /api/verify                 single email
POST   /api/bulk                   upload CSV/XLSX → job_id
GET    /api/bulk/{id}              poll status
GET    /api/bulk/{id}/download     download results CSV
DELETE /api/bulk/{id}              delete a job + its results
POST   /api/bulk/clear             admin — delete all non-running jobs

GET    /api/cache/export           export full cache to CSV
DELETE /api/cache/{id}             delete one cache row
POST   /api/cache/purge            delete expired rows
POST   /api/cache/clear            admin — delete every cache row

GET    /api/stats                  verdict + cache aggregates
GET    /api/domain/{domain}        domain reputation summary
GET    /api/health                 status + db_ok + enabled providers
```

---

## Tests & lint

```bash
pytest -q                          # 26 tests, all external HTTP mocked
ruff check .
bash scripts/pre_push_check.sh     # pre-push gate
```

---

## Notes

- All routes auth-gated. Sessions: SHA-256-hashed tokens in DB, HttpOnly cookie. New registrations start inactive — admin must approve. Password change/reset revokes every existing session.
- Bulk + single-verify endpoints are ownership-scoped — a user only sees their own jobs; admin/superadmin sees all. `Job.user_id` is stamped on creation.
- Per-IP rate limits on `/login`, `/forgot-password`, `/register`. Failed-login lockout still applies per-account.
- Origin-check + security-headers middleware (HSTS in prod) provide CSRF / clickjacking / sniffing defence on top of `samesite="lax"`.
- Timestamps render in **IST** (UTC+5:30) — DB stays naive-UTC, conversion is display-only via `app/templating.py`.
- Bulk jobs show a live **ETA** while running, plus a global HTMX progress bar for every in-flight request. Job list auto-refreshes while anything is queued or running.
- Free-tier safe: `keep_warm.yml` pings `/api/health` every 5 min so Neon doesn't auto-pause; lifespan + dashboard aggregates are bounded so cold starts can't 504.
- **Read [PROJECT_LOG.md](PROJECT_LOG.md) before changing anything** — has the do-not-regress list, env-var table, and the Workflow Runbook for triaging Vercel + GHA failures.
