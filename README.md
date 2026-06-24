# Email Validator

Multi-provider email validation web app. Validates single emails or bulk CSVs using Bouncify, ZeroBounce, NeverBounce, Hunter.io, and a free local stack — with caching, analytics, and a clean UI.

---

## Features

- **Single email validation** with live results and confidence score
- **Bulk CSV upload** — drag-and-drop, background processing, smart export (filter by verdict)
- **4 validation strategies** — Bouncify Only, Local First (saves credits), Consensus, Waterfall
- **5 providers** — Bouncify, ZeroBounce, NeverBounce, Hunter.io, Local (free, always on)
- **30-day result cache** — zero credits re-validating the same email twice
- **Analytics dashboard** — verdict trends, top invalid domains, cache stats (Chart.js)
- **REST API** — full OpenAPI docs at `/docs`
- **Dark mode** — persisted via localStorage
- **Vercel-ready** — Mangum ASGI adapter, auto `/tmp` paths

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI + Python 3.12 |
| Async HTTP | httpx |
| ORM / DB | SQLModel + SQLite |
| Frontend | HTMX + Tailwind CDN + Jinja2 |
| Charts | Chart.js 4.4 |
| Serverless | Mangum (Vercel) |
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

# 3. Set API key
cp .env.example .env
# Edit .env and add: BOUNCIFY_API_KEY=your_key_here

# 4. Run
python -m uvicorn app.main:app --reload

# 5. Open
open http://localhost:8000
```

---

## Vercel Deployment

```bash
npm i -g vercel
vercel --prod
```

Set environment variable in Vercel dashboard:

```
BOUNCIFY_API_KEY=your_key_here
```

The app auto-detects `VERCEL=1` and switches SQLite to `/tmp/email_validator.db` and uploads to `/tmp/uploads`.

> **Note:** Vercel Hobby has a 10s function timeout. Keep bulk CSVs under ~50 emails on Hobby. Use Pro (60s) or Railway/Render for larger files.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOUNCIFY_API_KEY` | Yes | Primary provider |
| `ZEROBOUNCE_API_KEY` | No | Enables ZeroBounce |
| `NEVERBOUNCE_API_KEY` | No | Enables NeverBounce |
| `HUNTER_API_KEY` | No | Enables Hunter.io |
| `DATABASE_URL` | No | Override SQLite path (e.g. for Postgres) |
| `UPLOAD_DIR` | No | Override upload directory |
| `HTTPX_TIMEOUT` | No | HTTP timeout in seconds (default: 10) |
| `MAX_BULK_EMAILS` | No | Hard cap on CSV rows (default: 0 = unlimited) |
| `CACHE_TTL_DAYS` | No | Result cache lifetime (default: 30) |
| `ENABLE_SMTP_PROBE` | No | Enable raw SMTP verification (default: false) |

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
  "strategy": "local_first"
}
```

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
├── db.py                # SQLModel engine, auto /tmp on Vercel
├── models.py            # Job, EmailResult, EmailCache tables
├── schemas.py           # Pydantic request/response DTOs
├── providers/           # bouncify, zerobounce, neverbounce, hunter, local
├── core/                # validator.py (strategies), csv_io.py, cache.py
├── routes/              # api_single, api_bulk, api_stats, health, ui
├── workers/             # bulk_worker.py (background processing)
└── templates/           # Jinja2 HTML (base, dashboard, validate, cache, analytics, settings, jobs)
api/
└── index.py             # Mangum handler for Vercel
samples/
└── sample_emails.csv    # 15 test emails (valid, invalid, disposable, role, malformed)
```
