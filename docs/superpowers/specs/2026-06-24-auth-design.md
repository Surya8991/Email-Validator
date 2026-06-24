# Auth + Design Improvements — Spec
Date: 2026-06-24

## Overview

Add session-based authentication and role-based access control to the FastAPI + Jinja2 email validator app. Two roles: `admin` and `user`. Shared data model — all users see all validation results; auth is purely access control. Admin gets a separate `/admin/*` section with its own dark sidebar.

---

## Data Model

### New table: `User`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `email` | str (unique) | login identifier |
| `password_hash` | str | bcrypt via passlib, cost factor 12 |
| `role` | str | `admin` or `user` |
| `is_active` | bool | `False` = pending approval, cannot log in |
| `created_at` | datetime | set on insert |
| `last_login` | datetime | nullable, updated on successful login |

### New table: `Session`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `user_id` | UUID | FK → User.id |
| `token_hash` | str | SHA-256 of the raw token stored in cookie |
| `expires_at` | datetime | 7 days from creation |
| `created_at` | datetime | |

Raw session token (`secrets.token_urlsafe(32)`) lives only in the signed `HttpOnly` cookie. The DB stores only its SHA-256 hash — a DB leak does not expose valid tokens.

---

## Routes & Guards

### FastAPI dependency guards

```python
require_auth   # any active logged-in user — else redirect /login
require_admin  # admin role + is_active=True — else 403
```

### New public routes (no auth)

| Route | Method | Purpose |
|---|---|---|
| `/login` | GET, POST | Split-panel login form |
| `/register` | GET, POST | Registration form; on submit shows "pending approval" message, no redirect |
| `/logout` | POST | Clears cookie, deletes session row |

### Existing routes — add `require_auth`

`/`, `/validate`, `/cache`, `/analytics`, `/jobs`, `/job/{id}`, `/settings`

### New admin routes — all require `require_admin`

| Route | Method | Purpose |
|---|---|---|
| `/admin` | GET | System overview: stats, user count, provider health |
| `/admin/users` | GET, POST | User list + create-user form |
| `/admin/users/{id}/activate` | POST | Approve pending user |
| `/admin/users/{id}/deactivate` | POST | Suspend active user |
| `/admin/usage` | GET | Per-user validation counts + per-provider credit breakdown |
| `/admin/providers` | GET, POST | API key config (moved from `/settings`) |

`/settings` retains strategy defaults and display prefs; provider config section removed and moved to `/admin/providers`.

---

## UI & Pages

### Login `/login`
- Split panel: indigo gradient brand panel left, form right
- Fields: Email, Password
- "Sign in" primary button
- Link to `/register` at bottom of form panel

### Register `/register`
- Same split-panel shell as login
- Fields: Email, Password, Confirm Password
- On submit: inline success message "Account created — waiting for admin approval." No redirect to app.

### Top nav changes
- Avatar circle (user initials, indigo background) added to right side of nav
- Click → dropdown: greyed email address, Sign out button
- Dark mode toggle stays in the nav bar (unchanged)
- Admin users see a `🛡 Admin` tab added to the main nav link row
- Non-admin users: no Admin tab visible

### Admin section `/admin/*`
Separate page shell — does not use `base.html`. Has its own layout:
- Top bar: `✉ EmailValidator` wordmark + purple `ADMIN` badge + `← Back to App` link
- Left sidebar (`#1e1b4b` dark indigo): links to Users · System Stats · Usage · Providers

**Users page** (`/admin/users`):
- Table: email, role, status badge (active/pending/suspended), created date, last login, actions
- Inline approve / deactivate buttons per row
- "Create User" button → modal or inline form: email, password, role selector

**System Stats** (`/admin`):
- Same metric cards + charts as `/analytics` but system-wide
- Additional: "validations per user" bar chart

**Usage** (`/admin/usage`):
- Table: user · total validations · per-provider call counts · last active date

**Providers** (`/admin/providers`):
- Provider config cards moved from `/settings`
- Shows API key status (set/not set), daily cap, enable/disable toggle

---

## Security

- Passwords: `passlib[bcrypt]`, cost factor 12
- Session token: `secrets.token_urlsafe(32)`, stored as `hashlib.sha256` hex digest
- Cookie: `HttpOnly=True`, `SameSite=Lax`, `Secure=True` when `ENVIRONMENT=production`
- Session TTL: 7 days sliding (expiry extended on each valid request)
- Old sessions for a user are deleted on new login (no session accumulation)
- `is_active=False` users are rejected at guard level regardless of correct password
- No rate limiting (acceptable for small internal team; add later if needed)

### First-run bootstrap

If `User` table is empty at startup, create one admin from env vars:
```
SECRET_KEY=<random 32+ char string>
ADMIN_EMAIL=you@company.com
ADMIN_PASSWORD=<change after first login>
```
Bootstrap skipped if any user already exists.

---

## New Dependencies

```
passlib[bcrypt]>=1.7.4
```
`itsdangerous` is already included via Starlette/FastAPI.

---

## New Templates

| Template | Purpose |
|---|---|
| `auth/login.html` | Split-panel login page |
| `auth/register.html` | Split-panel register page |
| `admin/base.html` | Admin shell (dark sidebar layout) |
| `admin/users.html` | User management page |
| `admin/stats.html` | System-wide analytics |
| `admin/usage.html` | Per-user usage table |
| `admin/providers.html` | Provider config (moved from settings) |

---

## New Source Files

| File | Purpose |
|---|---|
| `app/auth.py` | `require_auth`, `require_admin` dependencies; session read/write helpers |
| `app/routes/auth_routes.py` | Login, register, logout route handlers |
| `app/routes/admin.py` | All `/admin/*` route handlers |
