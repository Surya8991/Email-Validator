import hashlib
import json
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.auth import require_admin, require_superadmin
from app.config import settings
from app.db import engine, is_postgres
from app.services.email import send_account_approved_email, send_invite_email
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

INVITE_TTL_DAYS = 7

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


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

    chart_data = {
        "verdict_counts": verdict_counts,
        "daily_stats": [{"date": d, **counts} for d, counts in sorted(daily.items())],
        "total_validated": total_results,
        "total_cached": total_cache,
    }
    return templates.TemplateResponse(request, "admin/stats.html", {
        **_admin_ctx("stats", current_user),
        "total_results": total_results,
        "total_cache": total_cache,
        "total_users": total_users,
        "pending_users": pending_users,
        "verdict_counts": verdict_counts,
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
        base_url = str(request.base_url).rstrip("/")
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
        if user:
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

    base_url = str(request.base_url).rstrip("/")
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

    return RedirectResponse(
        url=(
            f"/admin/users?invite_url={invite_url}"
            f"&invite_email={email}&invite_mail={mail_status}"
        ),
        status_code=302,
    )


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


@router.get("/usage", response_class=HTMLResponse)
async def admin_usage(request: Request, current_user: User = Depends(require_admin)):
    with Session(engine) as db:
        users = db.exec(select(User).order_by(User.email)).all()

        # Jobs per user
        job_counts: dict[int, int] = {}
        emails_processed: dict[int, int] = {}
        for u in users:
            if u.id is None:
                continue
            count = db.exec(
                select(func.count()).select_from(Job).where(Job.user_id == u.id)
            ).one() or 0
            job_counts[u.id] = count
            total_emails = db.exec(
                select(func.sum(Job.processed)).where(Job.user_id == u.id)
            ).one() or 0
            emails_processed[u.id] = total_emails

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
        "provider_totals": provider_totals,
    })


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
        if user and user.id != current_user.id and user.role == "admin":
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
            db.add(Team(name=name, description=description.strip(), created_by=current_user.id))
            db.commit()
    return RedirectResponse(url="/admin/teams", status_code=302)


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


@router.post("/teams/{team_id}/approve/{membership_id}")
async def admin_approve_membership(
    team_id: int, membership_id: int, current_user: User = Depends(require_admin)
):
    with Session(engine) as db:
        m = db.get(TeamMembership, membership_id)
        if m and m.team_id == team_id and m.status == "pending":
            m.status = "active"
            m.approved_at = datetime.utcnow()
            m.approved_by = current_user.id
            db.commit()
    return RedirectResponse(url=f"/admin/teams/{team_id}", status_code=302)


@router.post("/teams/{team_id}/reject/{membership_id}")
async def admin_reject_membership(
    team_id: int, membership_id: int, current_user: User = Depends(require_admin)
):
    with Session(engine) as db:
        m = db.get(TeamMembership, membership_id)
        if m and m.team_id == team_id and m.status == "pending":
            m.status = "rejected"
            db.commit()
    return RedirectResponse(url=f"/admin/teams/{team_id}", status_code=302)


@router.post("/teams/{team_id}/remove/{user_id}")
async def admin_remove_member(
    team_id: int, user_id: int, current_user: User = Depends(require_admin)
):
    with Session(engine) as db:
        m = db.exec(
            select(TeamMembership)
            .where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
        ).first()
        if m:
            db.delete(m)
            db.commit()
    return RedirectResponse(url=f"/admin/teams/{team_id}", status_code=302)


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

@router.get("/audit-log", response_class=HTMLResponse)
async def admin_audit_log(
    request: Request,
    page: int = 1,
    action_filter: str = "",
    current_user: User = Depends(require_admin),
):
    limit = 50
    offset = (page - 1) * limit
    with Session(engine) as db:
        q = select(AuditLog).order_by(AuditLog.created_at.desc())  # type: ignore[arg-type]
        if action_filter:
            q = q.where(AuditLog.action.contains(action_filter))
        logs = db.exec(q.offset(offset).limit(limit)).all()
        total = db.exec(
            select(func.count()).select_from(AuditLog)
        ).one() or 0
    return templates.TemplateResponse(request, "admin/audit_log.html", {
        **_admin_ctx("audit", current_user),
        "logs": logs,
        "page": page,
        "total": total,
        "pages": max(1, (total + limit - 1) // limit),
        "action_filter": action_filter,
        "limit": limit,
    })


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
