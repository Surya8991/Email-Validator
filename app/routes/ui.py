import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.auth import require_auth
from app.config import settings
from app.db import engine, is_postgres
from app.models import EmailCache, EmailResult, Job, Team, TeamMembership, User
from app.providers.registry import get_enabled_providers

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_STRATEGIES = [
    {
        "value": "bouncify_only",
        "name": "Bouncify Only",
        "desc": "Fastest. Single provider, 1 API credit.",
        "cost": "$",
        "icon": "⚡",
    },
    {
        "value": "local_first",
        "name": "Local First",
        "desc": "Free local check first. Skip paid API on clear invalids.",
        "cost": "¢",
        "icon": "🏠",
    },
    {
        "value": "consensus",
        "name": "Consensus",
        "desc": "All providers in parallel. Most accurate, most credits.",
        "cost": "$$$",
        "icon": "🗳",
    },
    {
        "value": "waterfall",
        "name": "Waterfall",
        "desc": "Cascade providers, stop at first confident verdict.",
        "cost": "$$",
        "icon": "🌊",
    },
]


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, current_user: User = Depends(require_auth)):
    with Session(engine) as session:
        total_results = session.exec(select(func.count()).select_from(EmailResult)).one() or 0
        total_cache = session.exec(select(func.count()).select_from(EmailCache)).one() or 0

        verdict_rows = session.execute(text(
            "SELECT verdict, COUNT(*) FROM emailresult GROUP BY verdict"
        )).fetchall()
        verdict_counts = {r[0]: r[1] for r in verdict_rows}

        recent_jobs = session.exec(
            select(Job).order_by(Job.id.desc()).limit(5)  # type: ignore[arg-type]
        ).all()

    cache_rate = round(total_cache / total_results * 100, 1) if total_results > 0 else 0
    return templates.TemplateResponse(request, "dashboard.html", {
        "total_results": total_results,
        "total_cache": total_cache,
        "cache_rate": cache_rate,
        "verdict_counts": verdict_counts,
        "recent_jobs": recent_jobs,
        "enabled_providers": get_enabled_providers(),
        "active_page": "dashboard",
        "current_user": current_user,
    })


@router.get("/validate", response_class=HTMLResponse)
async def validate_page(request: Request, current_user: User = Depends(require_auth)):
    return templates.TemplateResponse(request, "validate.html", {
        "enabled_providers": get_enabled_providers(),
        "strategies": _STRATEGIES,
        "active_page": "validate",
        "current_user": current_user,
    })


@router.get("/cache", response_class=HTMLResponse)
async def cache_browser(request: Request, current_user: User = Depends(require_auth)):
    return templates.TemplateResponse(request, "cache.html", {
        "active_page": "cache",
        "current_user": current_user,
    })


@router.get("/partials/cache-table", response_class=HTMLResponse)
async def cache_table_partial(request: Request, q: str = "", page: int = 1):
    limit = 25
    offset = (page - 1) * limit
    with Session(engine) as session:
        base_q = select(EmailCache).order_by(EmailCache.validated_at.desc())  # type: ignore[arg-type]
        if q:
            base_q = base_q.where(EmailCache.email.contains(q))
        rows = session.exec(base_q.offset(offset).limit(limit)).all()

        count_q = select(func.count()).select_from(EmailCache)
        if q:
            count_q = count_q.where(EmailCache.email.contains(q))
        total = session.exec(count_q).one() or 0

    return templates.TemplateResponse(request, "partials/cache_rows.html", {
        "rows": rows,
        "q": q,
        "page": page,
        "total": total,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
        "now": datetime.now(UTC).replace(tzinfo=None),
    })


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, current_user: User = Depends(require_auth)):
    with Session(engine) as session:
        total_results = session.exec(select(func.count()).select_from(EmailResult)).one() or 0
        total_cache = session.exec(select(func.count()).select_from(EmailCache)).one() or 0

        verdict_rows = session.execute(text(
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
            domain_sql = """
                SELECT SPLIT_PART(email, '@', 2) AS domain, COUNT(*) AS cnt
                FROM emailresult WHERE verdict = 'invalid'
                GROUP BY domain ORDER BY cnt DESC LIMIT 10
            """
        else:
            daily_sql = """
                SELECT strftime('%Y-%m-%d', created_at) AS d, verdict, COUNT(*) AS cnt
                FROM emailresult
                WHERE created_at >= date('now', '-13 days')
                GROUP BY d, verdict ORDER BY d
            """
            domain_sql = """
                SELECT substr(email, instr(email, '@') + 1) AS domain, COUNT(*) AS cnt
                FROM emailresult WHERE verdict = 'invalid'
                GROUP BY domain ORDER BY cnt DESC LIMIT 10
            """

        daily_rows = session.execute(text(daily_sql)).fetchall()
        daily: dict[str, dict[str, int]] = defaultdict(
            lambda: {"valid": 0, "invalid": 0, "risky": 0, "unknown": 0}
        )
        for date_str, verdict, cnt in daily_rows:
            daily[date_str][verdict] = cnt

        domain_rows = session.execute(text(domain_sql)).fetchall()

    chart_data = {
        "verdict_counts": verdict_counts,
        "daily_stats": [{"date": d, **counts} for d, counts in sorted(daily.items())],
        "top_domains": [{"domain": r[0], "count": r[1]} for r in domain_rows],
        "total_validated": total_results,
        "total_cached": total_cache,
    }
    return templates.TemplateResponse(request, "analytics.html", {
        "chart_data_json": json.dumps(chart_data),
        "total_results": total_results,
        "total_cache": total_cache,
        "verdict_counts": verdict_counts,
        "active_page": "analytics",
        "current_user": current_user,
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, current_user: User = Depends(require_auth)):
    provider_cfg = [
        {
            "name": "Bouncify",
            "env": "BOUNCIFY_API_KEY",
            "configured": bool(settings.bouncify_api_key),
            "daily_cap": settings.bouncify_daily_cap,
        },
        {
            "name": "ZeroBounce",
            "env": "ZEROBOUNCE_API_KEY",
            "configured": bool(settings.zerobounce_api_key),
            "daily_cap": settings.zerobounce_daily_cap,
        },
        {
            "name": "NeverBounce",
            "env": "NEVERBOUNCE_API_KEY",
            "configured": bool(settings.neverbounce_api_key),
            "daily_cap": settings.neverbounce_daily_cap,
        },
        {
            "name": "Hunter.io",
            "env": "HUNTER_API_KEY",
            "configured": bool(settings.hunter_api_key),
            "daily_cap": settings.hunter_daily_cap,
        },
    ]
    with Session(engine) as session:
        total_cache = session.exec(select(func.count()).select_from(EmailCache)).one() or 0

    return templates.TemplateResponse(request, "settings.html", {
        "provider_cfg": provider_cfg,
        "cache_ttl_days": settings.cache_ttl_days,
        "total_cache": total_cache,
        "smtp_probe": settings.enable_smtp_probe,
        "active_page": "settings",
        "current_user": current_user,
    })


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_list(request: Request, current_user: User = Depends(require_auth)):
    with Session(engine) as session:
        jobs = session.exec(select(Job).order_by(Job.id.desc()).limit(50)).all()  # type: ignore[arg-type]
    return templates.TemplateResponse(request, "jobs.html", {
        "jobs": jobs,
        "active_page": "jobs",
        "current_user": current_user,
    })


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int, current_user: User = Depends(require_auth)):
    with Session(engine) as session:
        job = session.get(Job, job_id)
        results = session.exec(
            select(EmailResult).where(EmailResult.job_id == job_id).limit(200)
        ).all()
    if not job:
        return HTMLResponse("Job not found", status_code=404)
    parsed = []
    for r in results:
        try:
            provider_data = json.loads(r.provider_data)
        except Exception:
            provider_data = {}
        parsed.append({"email": r.email, "verdict": r.verdict, "providers": provider_data})
    return templates.TemplateResponse(
        request, "job.html", {
            "job": job,
            "results": parsed,
            "active_page": "jobs",
            "current_user": current_user,
        }
    )


@router.get("/jobs/{job_id}/status", response_class=HTMLResponse)
async def job_status_partial(request: Request, job_id: int):
    with Session(engine) as session:
        job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("")
    pct = int((job.processed / job.total * 100) if job.total else 0)
    return templates.TemplateResponse(
        request, "partials/job_progress.html", {"job": job, "pct": pct}
    )


@router.get("/teams", response_class=HTMLResponse)
async def teams_page(request: Request, current_user: User = Depends(require_auth)):
    with Session(engine) as db:
        teams = db.exec(select(Team).order_by(Team.name)).all()  # type: ignore[arg-type]
        my_memberships: dict[int, str] = {}
        if current_user.id:
            rows = db.exec(
                select(TeamMembership).where(TeamMembership.user_id == current_user.id)
            ).all()
            my_memberships = {m.team_id: m.status for m in rows}
        member_counts: dict[int, int] = {}
        for t in teams:
            if t.id is None:
                continue
            member_counts[t.id] = db.exec(
                select(func.count()).select_from(TeamMembership)
                .where(TeamMembership.team_id == t.id, TeamMembership.status == "active")
            ).one() or 0
    return templates.TemplateResponse(request, "teams.html", {
        "active_page": "teams",
        "current_user": current_user,
        "teams": teams,
        "my_memberships": my_memberships,
        "member_counts": member_counts,
    })


@router.post("/teams/{team_id}/request")
async def request_team_join(team_id: int, current_user: User = Depends(require_auth)):
    with Session(engine) as db:
        team = db.get(Team, team_id)
        if not team:
            return RedirectResponse(url="/teams", status_code=302)
        existing = db.exec(
            select(TeamMembership)
            .where(TeamMembership.team_id == team_id, TeamMembership.user_id == current_user.id)
        ).first()
        if not existing:
            db.add(TeamMembership(team_id=team_id, user_id=current_user.id, status="pending"))
            db.commit()
    return RedirectResponse(url="/teams", status_code=302)


@router.post("/teams/{team_id}/cancel")
async def cancel_team_request(team_id: int, current_user: User = Depends(require_auth)):
    with Session(engine) as db:
        m = db.exec(
            select(TeamMembership)
            .where(TeamMembership.team_id == team_id, TeamMembership.user_id == current_user.id,
                   TeamMembership.status == "pending")
        ).first()
        if m:
            db.delete(m)
            db.commit()
    return RedirectResponse(url="/teams", status_code=302)
