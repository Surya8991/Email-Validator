import asyncio
import csv
import io
import json
import time
from collections import defaultdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.auth import require_auth
from app.config import settings
from app.db import engine, is_postgres
from app.models import EmailCache, EmailResult, Job, Team, TeamMembership, User
from app.providers.registry import get_enabled_providers
from app.templating import job_eta_seconds, templates

# Short-lived in-memory cache for the dashboard's expensive aggregate queries.
# 30s TTL is enough that consecutive loads (e.g. browser-tab reopen, navigation)
# don't re-COUNT the whole tables, but fresh enough that numbers feel live.
_DASHBOARD_CACHE: dict = {"ts": 0.0, "data": None}
_DASHBOARD_TTL = 30.0


_JOB_LIST_COLS = (
    Job.id, Job.status, Job.total, Job.processed, Job.created_at,
    Job.filename, Job.strategy, Job.error,
)

_VERDICT_KEYS = ("valid", "invalid", "risky", "unknown")


def _per_user_verdict_stats() -> list[dict]:
    """One JOIN GROUP BY user_id, verdict — returns sorted rows:
        [{user_id, user_email, jobs, total, valid, invalid, risky, unknown}, ...]
    Used by /jobs to render the per-user stats panel visible to everyone.
    Heavier than the per-job verdicts query because it scans every
    EmailResult; cap to top-30-by-total to keep page render cheap.
    """
    base = {k: 0 for k in _VERDICT_KEYS}
    with Session(engine) as session:
        # Per-user job + processed-emails counts (one query).
        ujob_rows = session.execute(
            select(Job.user_id, func.count(), func.coalesce(func.sum(Job.processed), 0))
            .group_by(Job.user_id)
        ).all()
        per_user_jobs = {
            int(uid): {"jobs": int(jc), "processed": int(em or 0)}
            for uid, jc, em in ujob_rows if uid is not None
        }
        # Per-user verdict distribution (one JOIN query).
        verdict_rows = session.execute(
            select(Job.user_id, EmailResult.verdict, func.count())
            .join(EmailResult, EmailResult.job_id == Job.id)
            .group_by(Job.user_id, EmailResult.verdict)
        ).all()
        per_user_verdicts: dict[int, dict[str, int]] = {}
        for uid, verdict, cnt in verdict_rows:
            if uid is None:
                continue
            d = per_user_verdicts.setdefault(int(uid), dict(base))
            if verdict in base:
                d[verdict] = int(cnt)
        # Resolve emails for the user_ids we have (one IN-query).
        user_ids = list(per_user_jobs.keys())
        if not user_ids:
            return []
        email_rows = session.execute(
            select(User.id, User.email).where(User.id.in_(user_ids))  # type: ignore[attr-defined]
        ).all()
        emails = {int(uid): em for uid, em in email_rows}
    out = []
    for uid, jstats in per_user_jobs.items():
        v = per_user_verdicts.get(uid, dict(base))
        total = sum(v.values())
        out.append({
            "user_id": uid,
            "user_email": emails.get(uid, "—"),
            "jobs": jstats["jobs"],
            "processed": jstats["processed"],
            "total": total,
            **v,
        })
    out.sort(key=lambda r: r["total"], reverse=True)
    return out[:30]


def _job_verdict_counts(session: Session, job_ids: list[int]) -> dict[int, dict[str, int]]:
    """One batched GROUP BY job_id, verdict — returns
    {job_id: {valid: N, invalid: N, risky: N, unknown: N}} with zeros filled.

    Used by /jobs to render per-row verdict chips and by /jobs/{id} to render
    the top summary card. Job.processed is total written; this query splits
    that total by verdict. EmailResult.job_id already has an index, so the
    GROUP BY is cheap even for 50 jobs at a time.
    """
    base = {k: 0 for k in _VERDICT_KEYS}
    out: dict[int, dict[str, int]] = {jid: dict(base) for jid in job_ids}
    if not job_ids:
        return out
    rows = session.execute(
        select(EmailResult.job_id, EmailResult.verdict, func.count())
        .where(EmailResult.job_id.in_(job_ids))  # type: ignore[attr-defined]
        .group_by(EmailResult.job_id, EmailResult.verdict)
    ).all()
    for jid, verdict, cnt in rows:
        if jid in out and verdict in base:
            out[jid][verdict] = int(cnt)
    return out


def _dashboard_aggregates() -> dict:
    """Run the dashboard's COUNT queries. Cheap on warm Neon, slow when cold —
    so we cache the result briefly and let callers use asyncio.to_thread to
    parallelize multiple stat lookups when needed."""
    with Session(engine) as session:
        total_results = session.exec(select(func.count()).select_from(EmailResult)).one() or 0
        total_cache = session.exec(select(func.count()).select_from(EmailCache)).one() or 0
        verdict_rows = session.execute(text(
            "SELECT verdict, COUNT(*) FROM emailresult GROUP BY verdict"
        )).fetchall()
        # Project columns explicitly — DO NOT select the full Job row.
        # Job.csv_data holds the entire uploaded CSV (can be MB per row);
        # SELECT * across 5-50 rows fetched all of it and 504'd /jobs and /
        # on cold-Neon. Listing pages never read csv_data, only the worker does.
        recent_rows = session.execute(
            select(*_JOB_LIST_COLS, Job.user_id, User.email)
            .join(User, User.id == Job.user_id, isouter=True)  # type: ignore[arg-type]
            .order_by(Job.id.desc()).limit(5)
        ).all()
        recent = [
            {"id": r[0], "status": r[1], "total": r[2], "processed": r[3],
             "created_at": r[4], "filename": r[5], "strategy": r[6], "error": r[7],
             "user_id": r[8], "user_email": r[9]}
            for r in recent_rows
        ]
    return {
        "total_results": total_results,
        "total_cache": total_cache,
        "verdict_counts": {r[0]: r[1] for r in verdict_rows},
        "recent_jobs": recent,
    }

router = APIRouter()

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
    now = time.monotonic()
    if _DASHBOARD_CACHE["data"] is not None and (now - _DASHBOARD_CACHE["ts"]) < _DASHBOARD_TTL:
        agg = _DASHBOARD_CACHE["data"]
    else:
        # Run the blocking aggregates off the event loop with a hard ceiling.
        # If they don't finish (cold DB + Vercel 10s budget), serve placeholders
        # rather than 504. The next request that lands after the DB has warmed
        # up will populate the cache for everyone.
        try:
            agg = await asyncio.wait_for(asyncio.to_thread(_dashboard_aggregates), timeout=6.0)
            _DASHBOARD_CACHE["ts"] = now
            _DASHBOARD_CACHE["data"] = agg
        except Exception:
            agg = _DASHBOARD_CACHE["data"] or {
                "total_results": 0, "total_cache": 0,
                "verdict_counts": {}, "recent_jobs": [],
            }

    total_results = agg["total_results"]
    total_cache = agg["total_cache"]
    cache_rate = round(total_cache / total_results * 100, 1) if total_results > 0 else 0
    return templates.TemplateResponse(request, "dashboard.html", {
        "total_results": total_results,
        "total_cache": total_cache,
        "cache_rate": cache_rate,
        "verdict_counts": agg["verdict_counts"],
        "recent_jobs": agg["recent_jobs"],
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


def _is_privileged(user: User) -> bool:
    return user.role in ("admin", "superadmin")


def _cache_verdict_stats() -> dict:
    """Total cache size + per-verdict counts. Drives the dashboard cards on
    /cache (both admin and user views). One GROUP BY query keyed off the
    indexed `verdict` column — cheap even on hundreds of thousands of rows.

    Note: 'unknown' verdicts are intentionally NEVER cached (see
    validator.py:34) so that count is always 0; we still surface the slot
    for visual completeness.
    """
    out = {"total": 0, "valid": 0, "invalid": 0, "risky": 0, "unknown": 0}
    with Session(engine) as session:
        rows = session.execute(
            select(EmailCache.verdict, func.count()).group_by(EmailCache.verdict)
        ).all()
    for v, cnt in rows:
        if v in out:
            out[v] = int(cnt)
        out["total"] += int(cnt)
    return out


@router.get("/cache", response_class=HTMLResponse)
async def cache_browser(request: Request, current_user: User = Depends(require_auth)):
    # The EmailCache table is shared across all users by design. Plain users
    # see a scoped "Your Recent Validations" view (last 5 from their own
    # bulk jobs); admins / superadmins get the full global cache browser.
    # Both surfaces get the verdict-count dashboard above the table.
    template = "cache.html" if _is_privileged(current_user) else "cache_user.html"
    try:
        cache_stats = await asyncio.wait_for(
            asyncio.to_thread(_cache_verdict_stats), timeout=4.0,
        )
    except (TimeoutError, Exception):  # noqa: BLE001
        cache_stats = {"total": 0, "valid": 0, "invalid": 0, "risky": 0, "unknown": 0}
    return templates.TemplateResponse(request, template, {
        "active_page": "cache",
        "current_user": current_user,
        "cache_stats": cache_stats,
    })


_VALID_CACHE_VERDICTS = {"valid", "invalid", "risky"}


@router.get("/partials/cache-table", response_class=HTMLResponse)
async def cache_table_partial(
    request: Request,
    q: str = "",
    verdict: str = "",
    page: int = 1,
    current_user: User = Depends(require_auth),
):
    if not _is_privileged(current_user):
        # The global cache table is admin-only. Plain users should be calling
        # /partials/my-recent-validations from cache_user.html instead.
        raise HTTPException(status_code=403, detail="Admin only")
    limit = 25
    offset = (page - 1) * limit
    verdict_q = verdict.strip().lower() if verdict.strip().lower() in _VALID_CACHE_VERDICTS else ""
    with Session(engine) as session:
        base_q = select(EmailCache).order_by(EmailCache.validated_at.desc())  # type: ignore[arg-type]
        if q:
            base_q = base_q.where(EmailCache.email.contains(q))
        if verdict_q:
            base_q = base_q.where(EmailCache.verdict == verdict_q)
        rows = session.exec(base_q.offset(offset).limit(limit)).all()

        count_q = select(func.count()).select_from(EmailCache)
        if q:
            count_q = count_q.where(EmailCache.email.contains(q))
        if verdict_q:
            count_q = count_q.where(EmailCache.verdict == verdict_q)
        total = session.exec(count_q).one() or 0

    return templates.TemplateResponse(request, "partials/cache_rows.html", {
        "rows": rows,
        "q": q,
        "verdict": verdict_q,
        "page": page,
        "total": total,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
        "now": datetime.now(UTC).replace(tzinfo=None),
    })


@router.get("/partials/my-recent-validations", response_class=HTMLResponse)
async def my_recent_validations_partial(
    request: Request,
    current_user: User = Depends(require_auth),
):
    """Last 5 EmailResult rows from this user's own bulk jobs.

    Single-verify results aren't logged per-user (no UserResult table), so
    this view only shows bulk-job results. Admins land here too if they
    follow this URL directly — privileged users normally see the full
    /cache page instead.
    """
    with Session(engine) as session:
        rows = session.execute(
            text(
                "SELECT e.email, e.verdict, e.created_at, j.id AS job_id, "
                "       j.filename AS job_filename "
                "FROM emailresult e "
                "JOIN job j ON j.id = e.job_id "
                "WHERE j.user_id = :uid "
                "ORDER BY e.created_at DESC "
                "LIMIT 5"
            ),
            {"uid": current_user.id},
        ).fetchall()

    return templates.TemplateResponse(request, "partials/my_recent_validations.html", {
        "rows": [dict(r._mapping) for r in rows],
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


_VALID_JOB_STATUSES = {"queued", "running", "done", "failed"}
_JOBS_PER_PAGE = 50


def _list_jobs_lightweight(
    status: str | None = None,
    owner: str | None = None,
    page: int = 1,
) -> tuple[list[dict], int]:
    """Same column-projection pattern as the dashboard. Keeps csv_data on Neon.

    Returns (rows, total) — total is the row count BEFORE pagination so the
    template can render page-of-pages controls accurately.
    """
    offset = max(0, (page - 1) * _JOBS_PER_PAGE)
    with Session(engine) as session:
        stmt = (
            select(*_JOB_LIST_COLS, Job.user_id, User.email)
            .join(User, User.id == Job.user_id, isouter=True)  # type: ignore[arg-type]
        )
        count_stmt = (
            select(func.count()).select_from(Job)
            .join(User, User.id == Job.user_id, isouter=True)  # type: ignore[arg-type]
        )
        if status and status in _VALID_JOB_STATUSES:
            stmt = stmt.where(Job.status == status)
            count_stmt = count_stmt.where(Job.status == status)
        if owner:
            stmt = stmt.where(User.email.contains(owner))  # type: ignore[attr-defined]
            count_stmt = count_stmt.where(User.email.contains(owner))  # type: ignore[attr-defined]
        rows = session.execute(
            stmt.order_by(Job.id.desc()).offset(offset).limit(_JOBS_PER_PAGE)
        ).all()
        total = session.execute(count_stmt).scalar() or 0
        job_ids = [int(r[0]) for r in rows]
        verdicts = _job_verdict_counts(session, job_ids)
    return (
        [
            {"id": r[0], "status": r[1], "total": r[2], "processed": r[3],
             "created_at": r[4], "filename": r[5], "strategy": r[6], "error": r[7],
             "user_id": r[8], "user_email": r[9],
             "verdicts": verdicts.get(int(r[0]), {k: 0 for k in _VERDICT_KEYS})}
            for r in rows
        ],
        int(total),
    )


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_list(
    request: Request,
    status: str = "",
    owner: str = "",
    page: int = 1,
    current_user: User = Depends(require_auth),
):
    # Wrap with same 6s ceiling as the dashboard. Cold Neon + a SELECT * that
    # used to pull csv_data was 504-ing this page on every cold start.
    # owner filter is admin-only — non-admins always see only their own jobs
    # via the unchanged data path (the table itself is global; ownership is
    # enforced at the per-job detail/delete routes).
    is_admin = current_user.role in ("admin", "superadmin")
    owner_q = owner.strip() if is_admin else ""
    status_q = status.strip().lower() if status.strip().lower() in _VALID_JOB_STATUSES else ""
    page = max(1, page)
    try:
        jobs, total = await asyncio.wait_for(
            asyncio.to_thread(_list_jobs_lightweight, status_q or None, owner_q or None, page),
            timeout=6.0,
        )
    except (TimeoutError, Exception):  # noqa: BLE001
        jobs, total = [], 0
    # Per-user verdict stats panel — visible to everyone, capped at top 30
    # users by total processed so the query stays cheap even with many users.
    # Wrapped in its own thread + timeout so a slow Neon doesn't sink /jobs.
    try:
        user_stats = await asyncio.wait_for(
            asyncio.to_thread(_per_user_verdict_stats), timeout=4.0,
        )
    except (TimeoutError, Exception):  # noqa: BLE001
        user_stats = []
    pages = max(1, (total + _JOBS_PER_PAGE - 1) // _JOBS_PER_PAGE)
    return templates.TemplateResponse(request, "jobs.html", {
        "jobs": jobs,
        "user_stats": user_stats,
        "active_page": "jobs",
        "current_user": current_user,
        "status_filter": status_q,
        "owner_filter": owner_q,
        "page": page,
        "pages": pages,
        "total": total,
        "limit": _JOBS_PER_PAGE,
    })


@router.get("/jobs/export")
def jobs_export(
    status: str = "",
    owner: str = "",
    current_user: User = Depends(require_auth),
):
    """CSV export of the job history. Honors the same status/owner filters as
    the page. Pulls ALL matching jobs (no 50-page cap) since CSVs are normally
    used for billing / compliance reports.
    """
    is_admin = current_user.role in ("admin", "superadmin")
    owner_q = owner.strip() if is_admin else ""
    status_q = status.strip().lower() if status.strip().lower() in _VALID_JOB_STATUSES else ""
    with Session(engine) as session:
        stmt = (
            select(*_JOB_LIST_COLS, Job.user_id, User.email)
            .join(User, User.id == Job.user_id, isouter=True)  # type: ignore[arg-type]
            .order_by(Job.id.desc())
        )
        if status_q:
            stmt = stmt.where(Job.status == status_q)
        if owner_q:
            stmt = stmt.where(User.email.contains(owner_q))  # type: ignore[attr-defined]
        rows = session.execute(stmt).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "job_id", "status", "total", "processed", "created_at",
        "filename", "strategy", "owner_email", "error",
    ])
    for r in rows:
        writer.writerow([
            r[0], r[1], r[2], r[3],
            r[4].isoformat() if r[4] else "",
            r[5] or "", r[6] or "", r[9] or "", r[7] or "",
        ])
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="jobs-{stamp}.csv"'},
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int, current_user: User = Depends(require_auth)):
    with Session(engine) as session:
        # Same reason — never load Job.csv_data for the detail page either.
        row = session.execute(
            select(*_JOB_LIST_COLS, Job.user_id, User.email)
            .join(User, User.id == Job.user_id, isouter=True)  # type: ignore[arg-type]
            .where(Job.id == job_id)
        ).first()
        if row:
            results = session.exec(
                select(EmailResult).where(EmailResult.job_id == job_id).limit(200)
            ).all()
            verdicts = _job_verdict_counts(session, [int(row[0])])[int(row[0])]
        else:
            results = []
            verdicts = {k: 0 for k in _VERDICT_KEYS}
    if not row:
        return HTMLResponse("Job not found", status_code=404)
    job = {
        "id": row[0], "status": row[1], "total": row[2], "processed": row[3],
        "created_at": row[4], "filename": row[5], "strategy": row[6], "error": row[7],
        "user_id": row[8], "user_email": row[9],
        "verdicts": verdicts,
    }
    parsed = []
    for r in results:
        try:
            provider_data = json.loads(r.provider_data)
        except Exception:
            provider_data = {}
        parsed.append({"email": r.email, "verdict": r.verdict, "providers": provider_data})
    pct = int((job["processed"] / job["total"] * 100) if job["total"] else 0)
    eta_seconds = job_eta_seconds(job["processed"], job["total"], job["created_at"]) \
        if job["status"] == "running" else None
    return templates.TemplateResponse(
        request, "job.html", {
            "job": job,
            "results": parsed,
            "pct": pct,
            "eta_seconds": eta_seconds,
            "active_page": "jobs",
            "current_user": current_user,
        }
    )


@router.get("/jobs/{job_id}/status", response_class=HTMLResponse)
async def job_status_partial(request: Request, job_id: int):
    # Polled every 2s while a job is queued/running — must NOT load csv_data.
    with Session(engine) as session:
        row = session.execute(
            select(*_JOB_LIST_COLS).where(Job.id == job_id)
        ).first()
    if not row:
        return HTMLResponse("")
    job = {
        "id": row[0], "status": row[1], "total": row[2], "processed": row[3],
        "created_at": row[4], "filename": row[5], "strategy": row[6], "error": row[7],
    }
    pct = int((job["processed"] / job["total"] * 100) if job["total"] else 0)
    eta_seconds = job_eta_seconds(job["processed"], job["total"], job["created_at"]) \
        if job["status"] == "running" else None
    return templates.TemplateResponse(
        request, "partials/job_progress.html",
        {"job": job, "pct": pct, "eta_seconds": eta_seconds},
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
