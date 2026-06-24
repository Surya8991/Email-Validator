import json
from collections import defaultdict
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import EmailCache, EmailResult, Job
from app.providers.registry import get_enabled_providers

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

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
        "desc": "Free local check first — skip paid API on clear invalids.",
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
async def dashboard(request: Request):
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
    })


@router.get("/validate", response_class=HTMLResponse)
async def validate_page(request: Request):
    return templates.TemplateResponse(request, "validate.html", {
        "enabled_providers": get_enabled_providers(),
        "strategies": _STRATEGIES,
        "active_page": "validate",
    })


@router.get("/cache", response_class=HTMLResponse)
async def cache_browser(request: Request):
    return templates.TemplateResponse(request, "cache.html", {
        "active_page": "cache",
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
async def analytics_page(request: Request):
    with Session(engine) as session:
        total_results = session.exec(select(func.count()).select_from(EmailResult)).one() or 0
        total_cache = session.exec(select(func.count()).select_from(EmailCache)).one() or 0

        verdict_rows = session.execute(text(
            "SELECT verdict, COUNT(*) FROM emailresult GROUP BY verdict"
        )).fetchall()
        verdict_counts = {r[0]: r[1] for r in verdict_rows}

        daily_rows = session.execute(text("""
            SELECT strftime('%Y-%m-%d', created_at) as d, verdict, COUNT(*) as cnt
            FROM emailresult
            WHERE created_at >= date('now', '-13 days')
            GROUP BY d, verdict ORDER BY d
        """)).fetchall()

        daily: dict[str, dict[str, int]] = defaultdict(
            lambda: {"valid": 0, "invalid": 0, "risky": 0, "unknown": 0}
        )
        for date_str, verdict, cnt in daily_rows:
            daily[date_str][verdict] = cnt

        domain_rows = session.execute(text("""
            SELECT substr(email, instr(email, '@') + 1) as domain, COUNT(*) as cnt
            FROM emailresult WHERE verdict = 'invalid'
            GROUP BY domain ORDER BY cnt DESC LIMIT 10
        """)).fetchall()

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
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
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
    })


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_list(request: Request):
    with Session(engine) as session:
        jobs = session.exec(select(Job).order_by(Job.id.desc()).limit(50)).all()  # type: ignore[arg-type]
    return templates.TemplateResponse(request, "jobs.html", {
        "jobs": jobs,
        "active_page": "jobs",
    })


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int):
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
