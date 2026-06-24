import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import bcrypt
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.auth import require_admin
from app.config import settings
from app.db import engine, is_postgres
from app.models import ApiUsage, EmailCache, EmailResult, Job, User
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
    provider_cfg = [
        {"name": "Bouncify", "env": "BOUNCIFY_API_KEY", "configured": bool(settings.bouncify_api_key), "daily_cap": settings.bouncify_daily_cap},
        {"name": "ZeroBounce", "env": "ZEROBOUNCE_API_KEY", "configured": bool(settings.zerobounce_api_key), "daily_cap": settings.zerobounce_daily_cap},
        {"name": "NeverBounce", "env": "NEVERBOUNCE_API_KEY", "configured": bool(settings.neverbounce_api_key), "daily_cap": settings.neverbounce_daily_cap},
        {"name": "Hunter.io", "env": "HUNTER_API_KEY", "configured": bool(settings.hunter_api_key), "daily_cap": settings.hunter_daily_cap},
    ]
    return templates.TemplateResponse(request, "admin/providers.html", {
        **_admin_ctx("providers", current_user),
        "provider_cfg": provider_cfg,
        "enabled_providers": get_enabled_providers(),
    })
