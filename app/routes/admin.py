import asyncio
import csv
import hashlib
import io
import json
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlencode

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.auth import require_admin, require_superadmin
from app.config import settings
from app.db import engine, is_postgres
from app.models import (
    ApiUsage,
    AuditLog,
    EmailCache,
    EmailResult,
    Job,
    SystemSetting,
    Team,
    TeamMembership,
    User,
    UserInvite,
    UserSession,
)
from app.providers.registry import get_enabled_providers
from app.services.email import (
    send_account_approved_email,
    send_invite_email,
    send_team_join_decided_email,
)
from app.templating import templates

INVITE_TTL_DAYS = 7

# Short-lived in-memory cache for the admin overview's aggregate queries.
# Same pattern as the user dashboard in app/routes/ui.py — without it, a cold
# Neon connection + 4 COUNTs + a 13-day GROUP BY blows past Vercel Hobby's
# 10s function limit and the page 504s.
_ADMIN_OVERVIEW_CACHE: dict = {"ts": 0.0, "data": None}
_ADMIN_OVERVIEW_TTL = 30.0

router = APIRouter(prefix="/admin")


def _admin_overview_aggregates() -> dict:
    """Run the admin overview's COUNT + GROUP BY queries.

    Heavy on a cold Neon connection — callers should wrap with
    asyncio.to_thread + wait_for so a slow DB can't 504 the request.
    """
    with Session(engine) as db:
        total_results = db.exec(select(func.count()).select_from(EmailResult)).one() or 0
        total_cache = db.exec(select(func.count()).select_from(EmailCache)).one() or 0
        total_users = db.exec(select(func.count()).select_from(User)).one() or 0
        pending_users = db.exec(
            select(func.count()).select_from(User).where(User.is_active == False)  # noqa: E712
        ).one() or 0

        verdict_rows = db.execute(text(
            "SELECT verdict, COUNT(*) FROM emailresult GROUP BY verdict"
        )).fetchall()
        verdict_counts = {r[0]: r[1] for r in verdict_rows}

        if is_postgres():
            daily_sql = """
                SELECT TO_CHAR(created_at, 'YYYY-MM-DD') AS d, verdict, COUNT(*) AS cnt
                FROM emailresult
                WHERE created_at >= NOW() - INTERVAL '13 days'
                GROUP BY d, verdict ORDER BY d
            """
        else:
            daily_sql = """
                SELECT strftime('%Y-%m-%d', created_at) AS d, verdict, COUNT(*) AS cnt
                FROM emailresult
                WHERE created_at >= date('now', '-13 days')
                GROUP BY d, verdict ORDER BY d
            """

        daily_rows = db.execute(text(daily_sql)).fetchall()
        daily: dict[str, dict[str, int]] = defaultdict(
            lambda: {"valid": 0, "invalid": 0, "risky": 0, "unknown": 0}
        )
        for date_str, verdict, cnt in daily_rows:
            daily[date_str][verdict] = cnt

    return {
        "total_results": total_results,
        "total_cache": total_cache,
        "total_users": total_users,
        "pending_users": pending_users,
        "verdict_counts": verdict_counts,
        "daily": {d: dict(v) for d, v in daily.items()},
    }


def _log_audit(
    action: str,
    actor: User,
    target_type: str = "",
    target_id: str = "",
    details: str = "",
    db: Session | None = None,
) -> None:
    log = AuditLog(
        action=action,
        actor_id=actor.id,
        actor_email=actor.email,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )
    if db is not None:
        db.add(log)
    else:
        with Session(engine) as s:
            s.add(log)
            s.commit()


def _get_setting(key: str, default: str = "", db: Session | None = None) -> str:
    def _fetch(s: Session) -> str:
        row = s.get(SystemSetting, key)
        return row.value if row else default
    if db is not None:
        return _fetch(db)
    with Session(engine) as s:
        return _fetch(s)


def _set_setting(key: str, value: str, db: Session) -> None:
    row = db.get(SystemSetting, key)
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.add(SystemSetting(key=key, value=value))


def _month_count(user_id: int, db: Session) -> int:
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sql = text(
        "SELECT COUNT(*) FROM emailresult e "
        "JOIN job j ON j.id = e.job_id "
        "WHERE j.user_id = :uid AND e.created_at >= :ms"
    )
    return db.execute(sql, {"uid": user_id, "ms": month_start}).scalar() or 0


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def _admin_ctx(active: str, current_user: User) -> dict:
    return {"admin_active": active, "current_user": current_user}


@router.get("", response_class=HTMLResponse)
async def admin_overview(request: Request, current_user: User = Depends(require_admin)):
    now = time.monotonic()
    if (
        _ADMIN_OVERVIEW_CACHE["data"] is not None
        and (now - _ADMIN_OVERVIEW_CACHE["ts"]) < _ADMIN_OVERVIEW_TTL
    ):
        agg = _ADMIN_OVERVIEW_CACHE["data"]
    else:
        # Off the event loop with a hard ceiling — on a cold Neon connection
        # the COUNTs + GROUP BY can take >10s and would 504 Vercel Hobby.
        # If we miss the window, fall back to last cached snapshot (or zeros
        # on first ever cold start) so the page renders instead of erroring.
        try:
            agg = await asyncio.wait_for(
                asyncio.to_thread(_admin_overview_aggregates), timeout=6.0,
            )
            _ADMIN_OVERVIEW_CACHE["ts"] = now
            _ADMIN_OVERVIEW_CACHE["data"] = agg
        except Exception:
            agg = _ADMIN_OVERVIEW_CACHE["data"] or {
                "total_results": 0, "total_cache": 0,
                "total_users": 0, "pending_users": 0,
                "verdict_counts": {}, "daily": {},
            }

    daily = agg["daily"]
    chart_data = {
        "verdict_counts": agg["verdict_counts"],
        "daily_stats": [{"date": d, **counts} for d, counts in sorted(daily.items())],
        "total_validated": agg["total_results"],
        "total_cached": agg["total_cache"],
    }
    return templates.TemplateResponse(request, "admin/stats.html", {
        **_admin_ctx("stats", current_user),
        "total_results": agg["total_results"],
        "total_cache": agg["total_cache"],
        "total_users": agg["total_users"],
        "pending_users": agg["pending_users"],
        "verdict_counts": agg["verdict_counts"],
        "chart_data_json": json.dumps(chart_data),
    })


@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    q: str = "",
    role_filter: str = "",
    status_filter: str = "",
    current_user: User = Depends(require_admin),
):
    with Session(engine) as db:
        query = select(User).order_by(User.created_at.desc())  # type: ignore[arg-type]
        if q:
            query = query.where(User.email.contains(q.strip().lower()))
        if role_filter in ("user", "admin", "superadmin"):
            query = query.where(User.role == role_filter)
        if status_filter == "active":
            query = query.where(User.is_active == True)  # noqa: E712
        elif status_filter == "inactive":
            query = query.where(User.is_active == False)  # noqa: E712
        users = db.exec(query).all()
        active_count = sum(1 for u in users if u.is_active)
        pending_count = sum(1 for u in users if not u.is_active)
        val_counts: dict[int, int] = {
            u.id: _month_count(u.id, db) for u in users if u.id is not None
        }
        now = datetime.utcnow()
        invites = db.exec(
            select(UserInvite)
            .where(UserInvite.used_at == None, UserInvite.expires_at > now)  # noqa: E711
            .order_by(UserInvite.created_at.desc())  # type: ignore[arg-type]
        ).all()
    invite_url = request.query_params.get("invite_url")
    invite_email = request.query_params.get("invite_email")
    invite_error = request.query_params.get("invite_error")
    invite_mail = request.query_params.get("invite_mail")
    return templates.TemplateResponse(request, "admin/users.html", {
        **_admin_ctx("users", current_user),
        "users": users,
        "active_count": active_count,
        "pending_count": pending_count,
        "invites": invites,
        "invite_url": invite_url,
        "invite_email": invite_email,
        "invite_error": invite_error,
        "invite_mail": invite_mail,
        "val_counts": val_counts,
        "q": q,
        "role_filter": role_filter,
        "status_filter": status_filter,
    })


@router.get("/users/export")
def admin_users_export(
    q: str = "",
    role_filter: str = "",
    status_filter: str = "",
    current_user: User = Depends(require_admin),
):
    """Export the user table as CSV. Honors the same filters as the page."""
    with Session(engine) as db:
        query = select(User).order_by(User.created_at.desc())  # type: ignore[arg-type]
        if q:
            query = query.where(User.email.contains(q.strip().lower()))
        if role_filter in ("user", "admin", "superadmin"):
            query = query.where(User.role == role_filter)
        if status_filter == "active":
            query = query.where(User.is_active == True)  # noqa: E712
        elif status_filter == "inactive":
            query = query.where(User.is_active == False)  # noqa: E712
        users = db.exec(query).all()
        rows_data = [
            (
                u.email,
                u.role,
                "active" if u.is_active else "inactive",
                u.created_at.isoformat() if u.created_at else "",
                u.last_login.isoformat() if u.last_login else "",
                u.validation_limit if u.validation_limit is not None else "",
                u.failed_login_count,
                u.locked_until.isoformat() if u.locked_until else "",
            )
            for u in users
        ]
        _log_audit(
            "users.export", current_user, "users", "",
            f"q={q} role={role_filter} status={status_filter} rows={len(rows_data)}", db,
        )
        db.commit()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "email", "role", "status", "created_at", "last_login",
        "validation_limit", "failed_login_count", "locked_until",
    ])
    writer.writerows(rows_data)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="users-{stamp}.csv"'},
    )


@router.post("/users")
async def admin_create_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    current_user: User = Depends(require_admin),
):
    with Session(engine) as db:
        existing = db.exec(select(User).where(User.email == email.strip().lower())).first()
        if existing:
            users = db.exec(select(User).order_by(User.created_at.desc())).all()  # type: ignore[arg-type]
            return templates.TemplateResponse(request, "admin/users.html", {
                **_admin_ctx("users", current_user),
                "users": users,
                "active_count": sum(1 for u in users if u.is_active),
                "pending_count": sum(1 for u in users if not u.is_active),
                "form_error": f"Email {email} already exists.",
            }, status_code=400)
        db.add(User(
            email=email.strip().lower(),
            password_hash=_hash_password(password),
            role=role if role in ("admin", "user") else "user",
            is_active=True,
        ))
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{user_id}/activate")
async def admin_activate_user(
    request: Request,
    user_id: int,
    current_user: User = Depends(require_admin),
):
    notify_email = None
    was_inactive = False
    with Session(engine) as db:
        user = db.get(User, user_id)
        if user:
            was_inactive = not user.is_active
            user.is_active = True
            _log_audit("user.activate", current_user, "user", str(user_id), user.email, db)
            db.commit()
            notify_email = user.email

    if was_inactive and notify_email and settings.smtp_host:
        base_url = (settings.base_url or str(request.base_url)).rstrip("/")
        try:
            await send_account_approved_email(notify_email, f"{base_url}/login")
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("Approval email failed: %s", e)
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{user_id}/deactivate")
async def admin_deactivate_user(
    user_id: int,
    current_user: User = Depends(require_admin),
):
    if user_id == current_user.id:
        return RedirectResponse(url="/admin/users", status_code=302)
    with Session(engine) as db:
        user = db.get(User, user_id)
        if not user:
            return RedirectResponse(url="/admin/users", status_code=302)
        # Never strip the last active superadmin out of the system.
        last_super = (
            user.role == "superadmin"
            and _count_active_superadmins(db, exclude_user_id=user.id) == 0
        )
        if last_super:
            return RedirectResponse(
                url="/admin/users?invite_error=last_superadmin", status_code=302,
            )
        user.is_active = False
        _log_audit("user.deactivate", current_user, "user", str(user_id), user.email, db)
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{user_id}/set-limit")
async def admin_set_user_limit(
    user_id: int,
    limit: int | None = Form(default=None),
    current_user: User = Depends(require_superadmin),
):
    with Session(engine) as db:
        user = db.get(User, user_id)
        if user:
            user.validation_limit = limit if (limit and limit > 0) else None
            _log_audit(
                "user.set_limit", current_user, "user", str(user_id),
                f"{user.email} limit={user.validation_limit}", db,
            )
            db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


# ── Invites ───────────────────────────────────────────────────────────────────

def _hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.post("/invite")
async def admin_send_invite(
    request: Request,
    email: str = Form(...),
    role: str = Form("user"),
    current_user: User = Depends(require_admin),
):
    email = email.strip().lower()
    role = role if role in ("user", "admin") else "user"
    # superadmin-only: can invite admins; plain admin can only invite users
    if role == "admin" and current_user.role != "superadmin":
        role = "user"

    with Session(engine) as db:
        # Don't invite existing users
        existing_user = db.exec(select(User).where(User.email == email)).first()
        if existing_user:
            return RedirectResponse(
                url="/admin/users?invite_error=already_exists", status_code=302
            )
        # Revoke any prior unused invite for same email
        old = db.exec(
            select(UserInvite).where(UserInvite.email == email, UserInvite.used_at == None)  # noqa: E711
        ).first()
        if old:
            db.delete(old)

        raw_token = secrets.token_urlsafe(32)
        db.add(UserInvite(
            email=email,
            token_hash=_hash_invite_token(raw_token),
            role=role,
            invited_by=current_user.id,
            expires_at=datetime.utcnow() + timedelta(days=INVITE_TTL_DAYS),
        ))
        _log_audit("user.invite.send", current_user, "invite", email, f"role={role}", db)
        db.commit()

    base_url = (settings.base_url or str(request.base_url)).rstrip("/")
    invite_url = f"{base_url}/invite/{raw_token}"

    # Try to deliver the invite via SMTP. If SMTP isn't configured or sending
    # fails, we still surface the link in the UI so the admin can hand-deliver.
    mail_status = "skipped"
    if settings.smtp_host:
        try:
            await send_invite_email(
                to_email=email,
                invite_url=invite_url,
                role=role,
                inviter_email=current_user.email,
            )
            mail_status = "sent"
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("Invite email send failed: %s", e)
            mail_status = "failed"

    # urlencode-everything — `email` is user input from Form(), and `&` in a
    # legitimate-shaped address (e.g. `foo&utm=x@bar.com`) would otherwise
    # split into extra query params downstream. Also flagged by CodeQL
    # (py/url-redirection) as a stored-redirect vector.
    qs = urlencode({
        "invite_url": invite_url,
        "invite_email": email,
        "invite_mail": mail_status,
    })
    return RedirectResponse(url=f"/admin/users?{qs}", status_code=302)


@router.post("/invites/{invite_id}/revoke")
async def admin_revoke_invite(
    invite_id: int,
    current_user: User = Depends(require_admin),
):
    with Session(engine) as db:
        invite = db.get(UserInvite, invite_id)
        if invite and not invite.used_at:
            _log_audit("user.invite.revoke", current_user, "invite", invite.email, "", db)
            db.delete(invite)
            db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/retry-unknowns")
async def admin_retry_unknowns(
    batch_size: int = 500,
    max_batches: int = 1,
    since_days: int = 0,
    providers: str = "bouncify",
    strategy: str = "bouncify_only",
    job_id: int | None = None,
    strikes: int = 3,
    num_buckets: int = 15,
    current_user: User = Depends(require_admin),
):
    """Fan out the retry_unknowns workflow across N parallel runs.

    Each dispatch processes exactly one hash bucket (bucket=K of=N), so
    the same email always lands in the same bucket — zero double-work
    across parallel runs even as rows resolve.

    GHA's 10-bucket concurrency group on the workflow caps in-flight at
    10; the remaining N-10 wait in GitHub's own queue and dequeue as runs
    finish. With num_buckets=15 and ~500 emails per bucket, one click
    covers ~7,500 emails at up to ~10× the throughput of a single
    sequential sweep.

    Returns 502 with the GitHub API error of the FIRST failed dispatch
    (subsequent ones aren't attempted) so the UI can surface it. 503
    when GITHUB_PAT is not configured.
    """
    import httpx as _httpx

    if not settings.github_pat or not settings.github_repo:
        raise HTTPException(
            status_code=503,
            detail="GITHUB_PAT / GITHUB_REPO not configured — can't dispatch the workflow.",
        )
    if num_buckets < 1 or num_buckets > 20:
        raise HTTPException(
            status_code=400,
            detail="num_buckets must be between 1 and 20 (per-click cap).",
        )

    try:
        owner, repo = settings.github_repo.split("/", 1)
    except ValueError:
        raise HTTPException(
            status_code=500,
            detail=f"GITHUB_REPO is malformed: {settings.github_repo!r}",
        )
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/"
        "actions/workflows/retry_unknowns.yml/dispatches"
    )

    base_inputs: dict[str, str] = {
        "batch_size": str(batch_size),
        "max_batches": str(max_batches),
        "since_days": str(since_days),
        "providers": providers,
        "strategy": strategy,
        "strikes": str(strikes),
        "bucket_of": str(num_buckets),
    }
    if job_id:
        base_inputs["job_id"] = str(job_id)

    dispatched: list[int] = []
    async with _httpx.AsyncClient(timeout=8.0) as client:
        # Refuse the whole fan-out if the GHA queue is already full —
        # 15 dispatches in a tight loop would otherwise blow past the cap.
        cap = settings.max_queued_workflow_runs
        if cap > 0:
            from app.routes.api_bulk import _count_queued_workflow_runs
            queued = await _count_queued_workflow_runs(client, "retry_unknowns.yml")
            if queued >= cap:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Retry queue is full: {queued} retry runs are already "
                        f"waiting (cap: {cap}). Wait for some to start before "
                        f"dispatching more."
                    ),
                )
        for bucket in range(num_buckets):
            inputs = dict(base_inputs, bucket=str(bucket))
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.github_pat}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"ref": "main", "inputs": inputs},
            )
            if resp.status_code != 204:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Dispatch {bucket}/{num_buckets} failed ({resp.status_code}): "
                        f"{resp.text[:200]}. {len(dispatched)} earlier dispatches did succeed."
                    ),
                )
            dispatched.append(bucket)

    return {
        "ok": True,
        "dispatched": len(dispatched),
        "buckets": dispatched,
        "in_flight_cap": 10,
        "queued": max(0, len(dispatched) - 10),
        "approx_emails": len(dispatched) * batch_size,
    }


@router.get("/account-cleanup", response_class=HTMLResponse)
async def account_cleanup_page(request: Request, current_user: User = Depends(require_admin)):
    """Big-CSV cleanup tool. The browser parses + filters the file; the
    server only answers small cache-verdict lookups. Keeps the 113k-row
    CRM export off Vercel's 4.5 MB request-body limit."""
    return templates.TemplateResponse(request, "admin/account_cleanup.html", {
        **_admin_ctx("account-cleanup", current_user),
        "google_oauth_client_id": settings.google_oauth_client_id,
        "google_sheets_target_id": settings.google_sheets_target_id,
    })


@router.post("/cache-lookup")
async def admin_cache_lookup(
    payload: dict,
    current_user: User = Depends(require_admin),
):
    """Batch cache verdict lookup for the Account Cleanup page.

    Body: {"emails": ["a@x.com", ...]}   (capped at 5,000 per call)
    Resp: {"verdicts": {"a@x.com": {"verdict": "valid", "validated_at": ".."}, ...}}

    Only emails that exist in the cache are returned — absence in the
    response means "not in cache" and the browser treats those rows as
    keep-untouched per the cleanup policy.

    The WHERE clause uses LOWER(email) to catch legacy mixed-case rows;
    the functional index ix_emailcache_email_lower (created by db.py at
    startup) keeps the query under Vercel Hobby's 10s ceiling.
    """
    emails = payload.get("emails") if isinstance(payload, dict) else None
    if not isinstance(emails, list):
        raise HTTPException(status_code=400, detail="`emails` must be a list")
    if len(emails) > 5000:
        raise HTTPException(status_code=400, detail="Max 5,000 emails per request")

    keys = list({
        e.strip().lower() for e in emails
        if isinstance(e, str) and e.strip()
    })
    if not keys:
        return {"verdicts": {}}

    with Session(engine) as db:
        rows = db.exec(
            select(EmailCache.email, EmailCache.verdict, EmailCache.validated_at)
            .where(func.lower(EmailCache.email).in_(keys))  # type: ignore[attr-defined]
        ).all()

    verdicts = {
        r[0].lower(): {
            "verdict": r[1],
            "validated_at": r[2].isoformat() if r[2] else None,
        }
        for r in rows
    }
    return {"verdicts": verdicts}


_USAGE_VERDICT_KEYS = ("valid", "invalid", "risky", "unknown")


@router.get("/usage", response_class=HTMLResponse)
async def admin_usage(request: Request, current_user: User = Depends(require_admin)):
    with Session(engine) as db:
        users = db.exec(select(User).order_by(User.email)).all()

        # Per-user aggregates batched into 3 queries (was N+1: 2 per user).
        job_counts: dict[int, int] = {}
        emails_processed: dict[int, int] = {}
        user_job_rows = db.execute(
            select(Job.user_id, func.count(), func.coalesce(func.sum(Job.processed), 0))
            .group_by(Job.user_id)
        ).all()
        for uid, jcount, emails in user_job_rows:
            if uid is None:
                continue
            job_counts[uid] = int(jcount)
            emails_processed[uid] = int(emails or 0)

        # Per-user verdict distribution across all their EmailResult rows.
        # One JOIN GROUP BY beats per-user round-trips even on small fleets.
        empty = {k: 0 for k in _USAGE_VERDICT_KEYS}
        verdict_counts: dict[int, dict[str, int]] = {u.id: dict(empty) for u in users if u.id}
        verdict_rows = db.execute(
            select(Job.user_id, EmailResult.verdict, func.count())
            .join(EmailResult, EmailResult.job_id == Job.id)
            .group_by(Job.user_id, EmailResult.verdict)
        ).all()
        for uid, verdict, cnt in verdict_rows:
            if uid in verdict_counts and verdict in empty:
                verdict_counts[uid][verdict] = int(cnt)

        # Provider usage totals
        usage_rows = db.exec(
            select(ApiUsage.provider, func.sum(ApiUsage.calls)).group_by(ApiUsage.provider)
        ).all()
        provider_totals = {r[0]: r[1] for r in usage_rows}

    return templates.TemplateResponse(request, "admin/usage.html", {
        **_admin_ctx("usage", current_user),
        "users": users,
        "job_counts": job_counts,
        "emails_processed": emails_processed,
        "verdict_counts": verdict_counts,
        "provider_totals": provider_totals,
    })


@router.get("/usage/export")
def admin_usage_export(current_user: User = Depends(require_admin)):
    """CSV of per-user activity + provider call totals (capacity-planning report)."""
    with Session(engine) as db:
        users = db.exec(select(User).order_by(User.email)).all()
        rows_data: list[tuple] = []
        for u in users:
            if u.id is None:
                continue
            jobs_n = db.exec(
                select(func.count()).select_from(Job).where(Job.user_id == u.id)
            ).one() or 0
            emails_n = db.exec(
                select(func.sum(Job.processed)).where(Job.user_id == u.id)
            ).one() or 0
            rows_data.append((
                u.email, u.role,
                "active" if u.is_active else "inactive",
                jobs_n, emails_n,
                u.last_login.isoformat() if u.last_login else "",
            ))
        usage_rows = db.exec(
            select(ApiUsage.provider, func.sum(ApiUsage.calls)).group_by(ApiUsage.provider)
        ).all()
        provider_totals = [(r[0], r[1]) for r in usage_rows]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["# section", "per_user_activity"])
    writer.writerow(["email", "role", "status", "jobs", "emails_processed", "last_login"])
    writer.writerows(rows_data)
    writer.writerow([])
    writer.writerow(["# section", "provider_totals"])
    writer.writerow(["provider", "total_calls"])
    writer.writerows(provider_totals)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="usage-{stamp}.csv"'},
    )


_USER_EMAILS_VERDICTS = {"all", "valid", "invalid", "risky", "unknown"}


@router.get("/users/{user_id}/emails.csv")
def admin_user_emails_export(
    user_id: int,
    verdict: str = "all",
    current_user: User = Depends(require_admin),
):
    """Download every EmailResult the given user has accumulated across all
    their bulk jobs. Admin-only — regular users use /api/bulk/{id}/download
    for their own per-job results.

    Columns: email, verdict, job_id, job_filename, created_at.
    Optional ?verdict=valid|invalid|risky|unknown filter narrows by verdict.
    """
    if verdict not in _USER_EMAILS_VERDICTS:
        raise HTTPException(status_code=400, detail="Invalid verdict filter.")
    with Session(engine) as db:
        owner = db.get(User, user_id)
        if not owner:
            raise HTTPException(status_code=404, detail="User not found")

        # int(user_id) is FastAPI-validated already; explicit cast keeps the
        # CodeQL taint analyzer quiet and matches the rest of the codebase.
        stmt = (
            select(
                EmailResult.email, EmailResult.verdict,
                EmailResult.job_id, Job.filename, EmailResult.created_at,
            )
            .join(Job, Job.id == EmailResult.job_id)
            .where(Job.user_id == int(user_id))
            .order_by(EmailResult.job_id.desc(), EmailResult.id.asc())  # type: ignore[arg-type]
        )
        if verdict != "all":
            stmt = stmt.where(EmailResult.verdict == verdict)
        rows = db.execute(stmt).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "verdict", "job_id", "job_filename", "created_at"])
    for em, v, jid, fname, created in rows:
        writer.writerow([
            em or "", v or "", jid or "",
            fname or "",
            created.isoformat() if created else "",
        ])
    safe_owner = (owner.email or f"user{user_id}").replace("@", "-at-").replace("/", "_")
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fname = f"{safe_owner}-emails{'-' + verdict if verdict != 'all' else ''}-{stamp}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/providers", response_class=HTMLResponse)
async def admin_providers(request: Request, current_user: User = Depends(require_admin)):
    def _p(name: str, env: str, key: str, cap: int) -> dict:
        return {"name": name, "env": env, "configured": bool(key), "daily_cap": cap}

    s = settings
    provider_cfg = [
        _p("Bouncify", "BOUNCIFY_API_KEY", s.bouncify_api_key, s.bouncify_daily_cap),
        _p("ZeroBounce", "ZEROBOUNCE_API_KEY", s.zerobounce_api_key, s.zerobounce_daily_cap),
        _p("NeverBounce", "NEVERBOUNCE_API_KEY", s.neverbounce_api_key, s.neverbounce_daily_cap),
        _p("Hunter.io", "HUNTER_API_KEY", s.hunter_api_key, s.hunter_daily_cap),
    ]
    return templates.TemplateResponse(request, "admin/providers.html", {
        **_admin_ctx("providers", current_user),
        "provider_cfg": provider_cfg,
        "enabled_providers": get_enabled_providers(),
    })


# ── Superadmin: user role promotion ──────────────────────────────────────────

def _count_active_superadmins(db: Session, exclude_user_id: int | None = None) -> int:
    q = select(func.count()).select_from(User).where(
        User.role == "superadmin", User.is_active == True  # noqa: E712
    )
    if exclude_user_id is not None:
        q = q.where(User.id != exclude_user_id)
    return db.exec(q).one() or 0


@router.post("/users/{user_id}/promote")
async def admin_promote_user(user_id: int, current_user: User = Depends(require_superadmin)):
    with Session(engine) as db:
        user = db.get(User, user_id)
        if user and user.role == "user":
            user.role = "admin"
            _log_audit("user.promote", current_user, "user", str(user_id), user.email, db)
            db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{user_id}/demote")
async def admin_demote_user(user_id: int, current_user: User = Depends(require_superadmin)):
    with Session(engine) as db:
        user = db.get(User, user_id)
        if not user or user.id == current_user.id:
            return RedirectResponse(url="/admin/users", status_code=302)
        # Never demote the last active superadmin — leaves the system unmanageable.
        last_super = (
            user.role == "superadmin"
            and _count_active_superadmins(db, exclude_user_id=user.id) == 0
        )
        if last_super:
            return RedirectResponse(
                url="/admin/users?invite_error=last_superadmin", status_code=302,
            )
        if user.role in ("admin", "superadmin"):
            user.role = "user"
            _log_audit("user.demote", current_user, "user", str(user_id), user.email, db)
            db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


# ── Teams ─────────────────────────────────────────────────────────────────────

@router.get("/teams", response_class=HTMLResponse)
async def admin_teams(request: Request, current_user: User = Depends(require_admin)):
    with Session(engine) as db:
        teams = db.exec(select(Team).order_by(Team.name)).all()  # type: ignore[arg-type]
        # pending requests count per team
        pending: dict[int, int] = {}
        member_count: dict[int, int] = {}
        for t in teams:
            if t.id is None:
                continue
            pending[t.id] = db.exec(
                select(func.count()).select_from(TeamMembership)
                .where(TeamMembership.team_id == t.id, TeamMembership.status == "pending")
            ).one() or 0
            member_count[t.id] = db.exec(
                select(func.count()).select_from(TeamMembership)
                .where(TeamMembership.team_id == t.id, TeamMembership.status == "active")
            ).one() or 0
    return templates.TemplateResponse(request, "admin/teams.html", {
        **_admin_ctx("teams", current_user),
        "teams": teams,
        "pending": pending,
        "member_count": member_count,
    })


@router.post("/teams")
async def admin_create_team(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    current_user: User = Depends(require_admin),
):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/admin/teams", status_code=302)
    with Session(engine) as db:
        existing = db.exec(select(Team).where(Team.name == name)).first()
        if not existing:
            team = Team(name=name, description=description.strip(), created_by=current_user.id)
            db.add(team)
            db.commit()
            db.refresh(team)
            # Creator is the team owner — auto-add as an active member with role="owner".
            db.add(TeamMembership(
                team_id=team.id,
                user_id=current_user.id,
                status="active",
                role="owner",
                approved_at=datetime.utcnow(),
                approved_by=current_user.id,
            ))
            _log_audit("team.create", current_user, "team", str(team.id), team.name, db)
            db.commit()
    return RedirectResponse(url="/admin/teams", status_code=302)


@router.post("/teams/{team_id}/edit")
async def admin_edit_team(
    team_id: int,
    name: str = Form(...),
    description: str = Form(""),
    current_user: User = Depends(require_admin),
):
    name = name.strip()
    if not name:
        return RedirectResponse(url=f"/admin/teams/{int(team_id)}", status_code=302)
    with Session(engine) as db:
        team = db.get(Team, team_id)
        if team:
            # Block rename collisions with another team.
            clash = db.exec(
                select(Team).where(Team.name == name, Team.id != team_id)
            ).first()
            if not clash:
                team.name = name
            team.description = description.strip()
            _log_audit("team.edit", current_user, "team", str(team_id), team.name, db)
            db.commit()
    return RedirectResponse(url=f"/admin/teams/{int(team_id)}", status_code=302)


@router.get("/teams/{team_id}", response_class=HTMLResponse)
async def admin_team_detail(
    request: Request, team_id: int, current_user: User = Depends(require_admin)
):
    with Session(engine) as db:
        team = db.get(Team, team_id)
        if not team:
            return HTMLResponse("Team not found", status_code=404)
        members_rows = db.exec(
            select(TeamMembership, User)
            .where(TeamMembership.team_id == team_id, TeamMembership.status == "active")
            .join(User, User.id == TeamMembership.user_id)  # type: ignore[arg-type]
        ).all()
        pending_rows = db.exec(
            select(TeamMembership, User)
            .where(TeamMembership.team_id == team_id, TeamMembership.status == "pending")
            .join(User, User.id == TeamMembership.user_id)  # type: ignore[arg-type]
        ).all()
        members = [{"membership": m, "user": u} for m, u in members_rows]
        pending = [{"membership": m, "user": u} for m, u in pending_rows]
    return templates.TemplateResponse(request, "admin/team_detail.html", {
        **_admin_ctx("teams", current_user),
        "team": team,
        "members": members,
        "pending": pending,
    })


async def _notify_team_decision(
    request: Request, db: Session, team_id: int, user_id: int, decision: str
) -> None:
    if not settings.smtp_host:
        return
    user = db.get(User, user_id)
    team = db.get(Team, team_id)
    if not user or not team:
        return
    base_url = (settings.base_url or str(request.base_url)).rstrip("/")
    try:
        await send_team_join_decided_email(user.email, team.name, decision, base_url)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("Team-join %s email failed: %s", decision, e)


@router.post("/teams/{team_id}/approve/{membership_id}")
async def admin_approve_membership(
    request: Request, team_id: int, membership_id: int,
    current_user: User = Depends(require_admin),
):
    notify_user_id = None
    with Session(engine) as db:
        m = db.get(TeamMembership, membership_id)
        if m and m.team_id == team_id and m.status == "pending":
            m.status = "active"
            m.approved_at = datetime.utcnow()
            m.approved_by = current_user.id
            db.commit()
            notify_user_id = m.user_id
        if notify_user_id is not None:
            await _notify_team_decision(request, db, team_id, notify_user_id, "approved")
    return RedirectResponse(url=f"/admin/teams/{int(team_id)}", status_code=302)


@router.post("/teams/{team_id}/reject/{membership_id}")
async def admin_reject_membership(
    request: Request, team_id: int, membership_id: int,
    current_user: User = Depends(require_admin),
):
    notify_user_id = None
    with Session(engine) as db:
        m = db.get(TeamMembership, membership_id)
        if m and m.team_id == team_id and m.status == "pending":
            m.status = "rejected"
            db.commit()
            notify_user_id = m.user_id
        if notify_user_id is not None:
            await _notify_team_decision(request, db, team_id, notify_user_id, "rejected")
    return RedirectResponse(url=f"/admin/teams/{int(team_id)}", status_code=302)


@router.post("/teams/{team_id}/transfer/{user_id}")
async def admin_transfer_team_ownership(
    team_id: int, user_id: int, current_user: User = Depends(require_admin)
):
    with Session(engine) as db:
        target = db.exec(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id,
                TeamMembership.user_id == user_id,
                TeamMembership.status == "active",
            )
        ).first()
        # Target must be an existing active member, and not already the owner.
        if not target or target.role == "owner":
            return RedirectResponse(url=f"/admin/teams/{int(team_id)}", status_code=302)

        current_owner = db.exec(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id,
                TeamMembership.role == "owner",
            )
        ).first()
        if current_owner:
            current_owner.role = "member"
        target.role = "owner"
        _log_audit(
            "team.transfer_ownership", current_user, "team", str(team_id),
            f"new_owner_user_id={user_id}", db,
        )
        db.commit()
    return RedirectResponse(url=f"/admin/teams/{int(team_id)}", status_code=302)


@router.post("/teams/{team_id}/remove/{user_id}")
async def admin_remove_member(
    team_id: int, user_id: int, current_user: User = Depends(require_admin)
):
    with Session(engine) as db:
        m = db.exec(
            select(TeamMembership)
            .where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
        ).first()
        # Never silently remove the team owner — they must delete the team instead.
        if m and m.role != "owner":
            db.delete(m)
            db.commit()
    return RedirectResponse(url=f"/admin/teams/{int(team_id)}", status_code=302)


@router.post("/teams/{team_id}/delete")
async def admin_delete_team(team_id: int, current_user: User = Depends(require_admin)):
    with Session(engine) as db:
        team = db.get(Team, team_id)
        if team:
            memberships = db.exec(
                select(TeamMembership).where(TeamMembership.team_id == team_id)
            ).all()
            for m in memberships:
                db.delete(m)
            db.delete(team)
            db.commit()
    return RedirectResponse(url="/admin/teams", status_code=302)


# ── A1: Audit Log ─────────────────────────────────────────────────────────────

def _parse_iso_date(s: str) -> datetime | None:
    """Parse YYYY-MM-DD from the HTML5 <input type=date> field. Returns None if blank/bad."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _apply_audit_filters(stmt, *, action_filter, actor_filter, from_dt, to_dt):
    """Apply the four audit-log filters to a SELECT statement. Used by both
    the page route and the CSV export so they always agree on what's shown."""
    if action_filter:
        stmt = stmt.where(AuditLog.action.contains(action_filter))
    if actor_filter:
        stmt = stmt.where(AuditLog.actor_email.contains(actor_filter))
    if from_dt is not None:
        stmt = stmt.where(AuditLog.created_at >= from_dt)
    if to_dt is not None:
        # Inclusive end date — include all events on `to_dt` itself.
        stmt = stmt.where(AuditLog.created_at < to_dt + timedelta(days=1))
    return stmt


@router.get("/audit-log", response_class=HTMLResponse)
async def admin_audit_log(
    request: Request,
    page: int = 1,
    action_filter: str = "",
    actor_filter: str = "",
    from_date: str = "",
    to_date: str = "",
    current_user: User = Depends(require_admin),
):
    limit = 50
    page = max(1, page)
    offset = (page - 1) * limit
    from_dt = _parse_iso_date(from_date)
    to_dt = _parse_iso_date(to_date)
    with Session(engine) as db:
        q = _apply_audit_filters(
            select(AuditLog).order_by(AuditLog.created_at.desc()),  # type: ignore[arg-type]
            action_filter=action_filter, actor_filter=actor_filter,
            from_dt=from_dt, to_dt=to_dt,
        )
        logs = db.exec(q.offset(offset).limit(limit)).all()
        # FIX: previously the count query didn't apply filters, so paging was
        # wrong as soon as any filter was set.
        count_q = _apply_audit_filters(
            select(func.count()).select_from(AuditLog),
            action_filter=action_filter, actor_filter=actor_filter,
            from_dt=from_dt, to_dt=to_dt,
        )
        total = db.exec(count_q).one() or 0
    return templates.TemplateResponse(request, "admin/audit_log.html", {
        **_admin_ctx("audit", current_user),
        "logs": logs,
        "page": page,
        "total": total,
        "pages": max(1, (total + limit - 1) // limit),
        "action_filter": action_filter,
        "actor_filter": actor_filter,
        "from_date": from_date,
        "to_date": to_date,
        "limit": limit,
    })


@router.get("/audit-log/export")
async def admin_audit_log_export(
    action_filter: str = "",
    actor_filter: str = "",
    from_date: str = "",
    to_date: str = "",
    current_user: User = Depends(require_admin),
):
    """Export the audit log as CSV. Honors the same filters as the browser."""
    from_dt = _parse_iso_date(from_date)
    to_dt = _parse_iso_date(to_date)
    with Session(engine) as db:
        q = _apply_audit_filters(
            select(AuditLog).order_by(AuditLog.created_at.desc()),  # type: ignore[arg-type]
            action_filter=action_filter, actor_filter=actor_filter,
            from_dt=from_dt, to_dt=to_dt,
        )
        logs = db.exec(q).all()
        # Snapshot to plain tuples BEFORE commit — commit expires ORM attributes.
        rows_data = [
            (
                log.created_at.isoformat() if log.created_at else "",
                log.action,
                log.actor_email,
                log.target_type,
                log.target_id,
                log.details,
            )
            for log in logs
        ]
        _log_audit(
            "audit.export", current_user, "audit_log", "",
            (f"action={action_filter} actor={actor_filter} "
             f"from={from_date} to={to_date} rows={len(rows_data)}"), db,
        )
        db.commit()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "created_at", "action", "actor_email",
        "target_type", "target_id", "details",
    ])
    for row in rows_data:
        writer.writerow(row)

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    filename = f"audit-log-{stamp}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── A3: Session Manager (superadmin only) ─────────────────────────────────────

@router.get("/sessions", response_class=HTMLResponse)
async def admin_sessions(request: Request, current_user: User = Depends(require_superadmin)):
    with Session(engine) as db:
        now = datetime.utcnow()
        rows = db.exec(
            select(UserSession, User)
            .where(UserSession.expires_at > now)
            .join(User, User.id == UserSession.user_id)  # type: ignore[arg-type]
            .order_by(UserSession.expires_at.desc())  # type: ignore[arg-type]
        ).all()
        sessions = [{"session": s, "user": u} for s, u in rows]
    return templates.TemplateResponse(request, "admin/sessions.html", {
        **_admin_ctx("sessions", current_user),
        "sessions": sessions,
        "now": now,
    })


@router.post("/sessions/{session_id}/revoke")
async def admin_revoke_session(
    session_id: int,
    current_user: User = Depends(require_superadmin),
):
    with Session(engine) as db:
        s = db.get(UserSession, session_id)
        if s:
            target_user = db.get(User, s.user_id)
            _log_audit(
                "session.revoke", current_user, "session", str(session_id),
                target_user.email if target_user else "", db,
            )
            db.delete(s)
            db.commit()
    return RedirectResponse(url="/admin/sessions", status_code=302)


# ── A4: System Settings (superadmin only) ─────────────────────────────────────

_SETTING_KEYS = [
    ("registration_open", "1", "Open registration", "Allow new users to self-register"),
    ("maintenance_mode", "0", "Maintenance mode", "Show maintenance page to non-admins"),
    (
        "default_validation_limit", "", "Default monthly limit",
        "Applied to new users (blank = unlimited)",
    ),
]


@router.get("/sys-settings", response_class=HTMLResponse)
async def admin_sys_settings(request: Request, current_user: User = Depends(require_superadmin)):
    with Session(engine) as db:
        current: dict[str, str] = {
            key: _get_setting(key, default, db)
            for key, default, _, _ in _SETTING_KEYS
        }
    saved = request.query_params.get("saved") == "1"
    return templates.TemplateResponse(request, "admin/sys_settings.html", {
        **_admin_ctx("sys-settings", current_user),
        "setting_keys": _SETTING_KEYS,
        "current": current,
        "saved": saved,
    })


@router.post("/sys-settings")
async def admin_save_sys_settings(
    request: Request,
    current_user: User = Depends(require_superadmin),
):
    form = await request.form()
    with Session(engine) as db:
        for key, default, _, _ in _SETTING_KEYS:
            raw = str(form.get(key, default)).strip()
            _set_setting(key, raw, db)
        _log_audit("system.settings.update", current_user, "system", "settings", "", db)
        db.commit()
    return RedirectResponse(url="/admin/sys-settings?saved=1", status_code=302)
