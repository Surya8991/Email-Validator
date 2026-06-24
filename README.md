# Email Validator

Multi-provider email validation web app with auth, teams, and an admin panel. Validates single emails or bulk CSVs using Bouncify, ZeroBounce, NeverBounce, Hunter.io, and a free local stack — with caching, analytics, and a clean UI.

---

## Features

- **Session-based auth** — login, register, logout with HttpOnly cookies; 7-day sliding sessions
- **Three-tier roles** — `user` → `admin` → `superadmin` with gated access per role
- **Admin panel** (`/admin`) — manage users, teams, usage stats, provider config
- **Teams** — admins create teams; users request to join; admins approve/reject
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
| Auth | Session cookies + bcrypt + SHA-256 token hashing |
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
# Edit .env — add at minimum:
#   BOUNCIFY_API_KEY=...
#   SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
#   ADMIN_EMAIL=you@example.com
#   ADMIN_PASSWORD=yourpassword

# 4. Init DB (PostgreSQL only — skip for local SQLite)
python scripts/init_db.py

# 5. Run
python -m uvicorn app.main:app --reload

# 6. Open
open http://localhost:8000
# → redirects to /login — use ADMIN_EMAIL + ADMIN_PASSWORD
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
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_EMAIL` | first admin email (only used when User table is empty) |
| `ADMIN_PASSWORD` | first admin password |
| `SUPERADMIN_EMAIL` | promoted/created as superadmin on every startup |
| `PRODUCTION` | `true` |

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

Tables: `job`, `emailresult`, `emailcache`, `apiusage`, `user`, `usersession`, `team`, `teammembership`

---

## Auth

- All routes require login — unauthenticated requests redirect to `/login`
- New registrations start **inactive** — an admin must activate before the user can log in
- **Admin** (`/admin`): manage users, teams, stats — requires `admin` or `superadmin` role
- **Superadmin**: set `SUPERADMIN_EMAIL` env var — promoted on every startup (idempotent); can promote/demote admins

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOUNCIFY_API_KEY` | Yes | — | Primary validation provider |
| `DATABASE_URL` | Yes (Vercel) | SQLite locally | Neon connection string |
| `GITHUB_PAT` | For bulk | — | PAT with `Actions (write)` scope |
| `GITHUB_REPO` | For bulk | — | `owner/repo` of this repo |
| `SECRET_KEY` | Yes (prod) | dev placeholder | Random hex — `openssl rand -hex 32` |
| `ADMIN_EMAIL` | First deploy | — | Bootstrap first admin (runs once on empty DB) |
| `ADMIN_PASSWORD` | First deploy | — | Bootstrap first admin password |
| `SUPERADMIN_EMAIL` | Recommended | — | Promoted to superadmin on every startup |
| `PRODUCTION` | No | `false` | Enables stricter security defaults |
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

| Page | URL | Role |
|---|---|---|
| Login | `/login` | Public |
| Register | `/register` | Public |
| Dashboard | `/` | User+ |
| Validate | `/validate` | User+ |
| Cache Browser | `/cache` | User+ |
| Analytics | `/analytics` | User+ |
| History | `/jobs` | User+ |
| Teams | `/teams` | User+ |
| Settings | `/settings` | User+ |
| API Docs | `/docs` | User+ |
| Admin Overview | `/admin` | Admin+ |
| Admin Users | `/admin/users` | Admin+ |
| Admin Teams | `/admin/teams` | Admin+ |
| Admin Usage | `/admin/usage` | Admin+ |
| Admin Providers | `/admin/providers` | Admin+ |

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

**Response:**
```json
{
  "email": "user@example.com",
  "verdict": "valid",
  "confidence": 88,
  "cached": false,
  "providers": { "local": {...}, "bouncify": {...} },
  "elapsed_ms": 312.4
}
```

### Bulk CSV
```http
POST /api/bulk         # upload → job_id
GET  /api/bulk/{id}    # poll status
GET  /api/bulk/{id}/download?verdict=valid
```

### Stats & cache
```
GET  /api/stats
GET  /api/domain/{domain}
GET  /api/health
POST /api/cache/purge
DEL  /api/cache/{id}
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

## Running Tests

```bash
pytest -q   # 26 tests
```

Auth tests use an in-memory SQLite DB and a logged-in `auth_client` fixture — no real API calls.

---

## Development

```bash
ruff check .            # lint
ruff format .           # format
mypy app/               # type check
bash scripts/pre_push_check.sh   # 39-check pre-push gate (runs auto via git hook)
```

---

## Security

- All routes behind session auth — unauthenticated → redirect to `/login`
- Session tokens: SHA-256 hashed in DB, raw token only in HttpOnly SameSite=Lax cookie
- Never commit `.env` — it's gitignored
- `SECRET_KEY` must be a random hex string in production (not the dev placeholder)
- Registered users start inactive — admin approval required
- Per-provider daily caps to prevent runaway credit usage

---

## Project Structure

```
app/
├── main.py              # FastAPI app, lifespan, exception handlers, admin bootstrap
├── auth.py              # Session helpers, require_auth/admin/superadmin guards
├── config.py            # pydantic-settings, .env loader
├── db.py                # SQLModel engine, URL normalization
├── models.py            # All DB tables (Job, EmailResult, EmailCache, ApiUsage, User, UserSession, Team, TeamMembership)
├── schemas.py           # Pydantic request/response DTOs
├── providers/           # bouncify, zerobounce, neverbounce, hunter, local, registry
├── core/                # validator.py, csv_io.py, cache.py, retry.py
├── routes/
│   ├── ui.py            # User-facing pages (auth-gated)
│   ├── auth_routes.py   # /login, /register, /logout
│   ├── admin.py         # /admin/* (admin/superadmin only)
│   ├── api_single.py    # POST /api/verify
│   ├── api_bulk.py      # POST /api/bulk + polling + download
│   ├── api_stats.py     # GET /api/stats, /api/domain, /api/cache
│   └── health.py        # GET /api/health
├── workers/             # bulk_worker.py
└── templates/
    ├── base.html        # Nav with avatar dropdown + admin tab
    ├── auth/            # login.html, register.html (split-panel)
    ├── admin/           # base.html, users.html, stats.html, usage.html, providers.html, teams.html, team_detail.html
    └── teams.html       # User-facing team cards
api/
└── index.py             # Mangum handler for Vercel
scripts/
├── init_db.py           # One-time DB table creation
├── process_job.py       # GitHub Actions bulk processor
└── pre_push_check.sh    # 39-check pre-push safety gate
```
