import json
from collections import defaultdict
from datetime import datetime
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
from app.models import ApiUsage, EmailCache, EmailResult, Job, Team, TeamMembership, User
from app.providers.registry import get_enabled_providers

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


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
async def admin_users(request: Request, current_user: User = Depends(require_admin)):
    with Session(engine) as db:
        users = db.exec(select(User).order_by(User.created_at.desc())).all()  # type: ignore[arg-type]
        active_count = sum(1 for u in users if u.is_active)
        pending_count = sum(1 for u in users if not u.is_active)
    return templates.TemplateResponse(request, "admin/users.html", {
        **_admin_ctx("users", current_user),
        "users": users,
        "active_count": active_count,
        "pending_count": pending_count,
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
    user_id: int,
    current_user: User = Depends(require_admin),
):
    with Session(engine) as db:
        user = db.get(User, user_id)
        if user:
            user.is_active = True
            db.commit()
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
            db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{user_id}/demote")
async def admin_demote_user(user_id: int, current_user: User = Depends(require_superadmin)):
    with Session(engine) as db:
        user = db.get(User, user_id)
        if user and user.id != current_user.id and user.role == "admin":
            user.role = "user"
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
