# Email Validator

Multi-provider email validation web app. Validates single emails or bulk CSVs using Bouncify, ZeroBounce, NeverBounce, Hunter.io, and a free local stack — with caching, analytics, and a clean UI.

---

## Features

- **Single email validation** with live results and confidence score
- **Bulk CSV upload** — drag-and-drop, processed via GitHub Actions (no timeout limit)
- **4 validation strategies** — Bouncify Only, Local First (saves credits), Consensus, Waterfall
- **5 providers** — Bouncify, ZeroBounce, NeverBounce, Hunter.io, Local (free, always on)
- **Per-result cache TTL** — choose how long each result is cached (or skip caching entirely)
- **Analytics dashboard** — verdict trends, top invalid domains, cache stats (Chart.js)
- **REST API** — full OpenAPI docs at `/docs`
- **Dark mode** — persisted via localStorage
- **Vercel-ready** — Mangum ASGI adapter, persistent PostgreSQL via Neon

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI + Python 3.12 |
| Async HTTP | httpx |
| ORM / DB | SQLModel + **PostgreSQL (Neon)** |
| Frontend | HTMX + Tailwind CDN + Jinja2 |
| Charts | Chart.js 4.4 |
| Serverless | Mangum (Vercel) |
| Bulk processing | GitHub Actions (bypasses Vercel 10s timeout) |
| Tests | pytest + pytest-asyncio + respx |
| Lint | ruff |

---

## Quick Start (Local)

```bash
# 1. Clone
git clone https://github.com/Layruss98266/Email-Validator.git
cd Email-Validator

# 2. Install deps
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — add BOUNCIFY_API_KEY at minimum
# Add DATABASE_URL for PostgreSQL (or leave blank for local SQLite)

# 4. Init DB (PostgreSQL only — skip for local SQLite)
python scripts/init_db.py

# 5. Run
python -m uvicorn app.main:app --reload

# 6. Open
open http://localhost:8000
```

---

## Vercel Deployment

### 1. Deploy

```bash
npm i -g vercel
vercel --prod
```

### 2. Set environment variables in Vercel dashboard

| Variable | Value |
|---|---|
| `BOUNCIFY_API_KEY` | your Bouncify key |
| `DATABASE_URL` | Neon connection string (`postgres://...`) |
| `GITHUB_PAT` | GitHub PAT with `Actions (write)` scope |
| `GITHUB_REPO` | `YourGitHubUsername/Email-Validator` |

### 3. Set GitHub repository secrets (for bulk processing)

Go to repo → Settings → Secrets → Actions:

| Secret | Value |
|---|---|
| `DATABASE_URL` | same Neon URL |
| `BOUNCIFY_API_KEY` | same key |

### 4. Init Neon tables (one-time)

Add `DATABASE_URL` to your local `.env`, then:

```bash
python scripts/init_db.py
```

> Bulk CSV jobs are processed by GitHub Actions — no timeout limit. The Vercel function only queues the job and triggers the workflow.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOUNCIFY_API_KEY` | Yes | — | Primary validation provider |
| `DATABASE_URL` | Yes (Vercel) | SQLite locally | Neon/Supabase/Railway Postgres URL |
| `GITHUB_PAT` | For bulk | — | PAT with `Actions (write)` scope |
| `GITHUB_REPO` | For bulk | — | `owner/repo` of this repo |
| `ZEROBOUNCE_API_KEY` | No | — | Enables ZeroBounce |
| `NEVERBOUNCE_API_KEY` | No | — | Enables NeverBounce |
| `HUNTER_API_KEY` | No | — | Enables Hunter.io |
| `CACHE_TTL_DAYS` | No | `30` | Default cache lifetime in days |
| `HTTPX_TIMEOUT` | No | `10.0` | HTTP timeout (keep ≤ 8 on Vercel Hobby) |
| `MAX_BULK_EMAILS` | No | `0` | Hard cap on CSV rows (0 = unlimited) |
| `ENABLE_SMTP_PROBE` | No | `false` | Raw SMTP verification (port 25 often blocked) |
| `BOUNCIFY_DAILY_CAP` | No | `500` | Max Bouncify calls/day (0 = unlimited) |

---

## Pages

| Page | URL | Description |
|---|---|---|
| Dashboard | `/` | Stats overview, quick validate, recent jobs |
| Validate | `/validate` | Single email or bulk CSV upload |
| Cache Browser | `/cache` | Search cached results, delete/purge |
| Analytics | `/analytics` | Charts: verdicts, trends, top domains |
| History | `/jobs` | All bulk jobs with progress and download |
| Settings | `/settings` | Provider status, domain reputation lookup |
| API Docs | `/docs` | Interactive OpenAPI (Swagger UI) |

---

## API Endpoints

### Verify single email
```http
POST /api/verify
Content-Type: application/json

{
  "email": "user@example.com",
  "providers": ["bouncify", "local"],
  "strategy": "local_first",
  "cache_ttl_days": 7
}
```

`cache_ttl_days`: `null` = use global default, `0` = skip caching, `N` = cache for N days.

**Response:**
```json
{
  "email": "user@example.com",
  "verdict": "valid",
  "confidence": 88,
  "cached": false,
  "providers": {
    "local": { "status": "valid", "is_disposable": false, "mx_found": true },
    "bouncify": { "status": "valid", "sub_status": "deliverable" }
  },
  "elapsed_ms": 312.4
}
```

### Bulk CSV upload
```http
POST /api/bulk
Content-Type: multipart/form-data

file=<CSV>
providers=bouncify,local
strategy=local_first
email_column=email   # optional, auto-detected
```

Returns `job_id`. Job is queued and processed by GitHub Actions asynchronously.

### Poll job status
```
GET /api/bulk/{job_id}
```

### Download results (with verdict filter)
```
GET /api/bulk/{job_id}/download?verdict=valid
```
Options: `all` (default), `valid`, `invalid`, `risky`

### Stats
```
GET /api/stats
GET /api/domain/{domain}
GET /api/health
```

### Cache management
```
POST /api/cache/purge       # delete all expired
DELETE /api/cache/{id}      # delete one entry
```

---

## Validation Strategies

| Strategy | Cost | Description |
|---|---|---|
| `bouncify_only` | $ | Single provider, fastest. 1 credit per email. |
| `local_first` | ¢ | Free local check first. API only if not clearly invalid. |
| `consensus` | $$$ | All providers in parallel, majority vote. Most accurate. |
| `waterfall` | $$ | Tries providers in order, stops at first confident verdict. |

---

## Bulk CSV Format

Any CSV with an `email` column (or use the first column). Extra columns are preserved in output.

```csv
email,name,source
user@example.com,Alice,signup
test@mailinator.com,Bob,import
```

**Output adds:** `verdict`, `from_cache`, `{provider}_status` per enabled provider.

Download a sample: [`/samples/sample_emails.csv`](/samples/sample_emails.csv)

---

## Verdicts

| Verdict | Meaning |
|---|---|
| `valid` | Email exists and can receive mail |
| `invalid` | Non-existent address or no MX record |
| `risky` | Catch-all, disposable, or role address — deliverable but risky |
| `unknown` | API error or inconclusive — not cached, retry later |

---

## Running Tests

```bash
pytest -q
```

All 25 tests use `respx` to mock provider HTTP calls — no real API credits consumed.

---

## Development

```bash
# Lint
ruff check .

# Type check
mypy app/

# Format
ruff format .

# Pre-push safety check (runs automatically via git hook)
bash scripts/pre_push_check.sh
```

---

## Security

- Never commit `.env` — it's gitignored
- All API keys loaded from environment only
- Cache stores email + verdict only — no PII in logs
- Per-provider daily caps configurable to prevent runaway credit usage

---

## Project Structure

```
app/
├── main.py              # FastAPI app, routes, custom /docs
├── config.py            # pydantic-settings, .env loader
├── db.py                # SQLModel engine, URL normalization (postgres:// → postgresql+psycopg2://)
├── models.py            # Job, EmailResult, EmailCache, ApiUsage tables
├── schemas.py           # Pydantic request/response DTOs
├── providers/           # bouncify, zerobounce, neverbounce, hunter, local, registry
├── core/                # validator.py (strategies), csv_io.py, cache.py, retry.py
├── routes/              # api_single, api_bulk, api_stats, health, ui
├── workers/             # bulk_worker.py (BackgroundTasks fallback for local dev)
└── templates/           # Jinja2 HTML (base, dashboard, validate, cache, analytics, settings, jobs)
api/
└── index.py             # Mangum handler for Vercel
scripts/
├── init_db.py           # One-time DB table creation (run against Neon)
├── process_job.py       # GitHub Actions bulk processor
└── pre_push_check.sh    # 26-check pre-push safety checklist
.github/
└── workflows/
    └── bulk_process.yml # Triggered by Vercel for bulk CSV jobs
samples/
└── sample_emails.csv    # 15 test emails (valid, invalid, disposable, role, malformed)
ruff.toml                # Ruff linter config
pytest.ini               # Pytest config (asyncio_mode=auto)
mypy.ini                 # Mypy strict config
```
