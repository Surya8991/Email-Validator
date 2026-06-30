import csv
import io
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.auth import require_auth
from app.core.cache import purge_expired
from app.db import engine, is_postgres
from app.models import EmailCache, EmailResult, User

router = APIRouter()

# Dialect-aware SQL fragments
def _daily_sql() -> str:
    if is_postgres():
        return """
            SELECT TO_CHAR(created_at, 'YYYY-MM-DD') AS d, verdict, COUNT(*) AS cnt
            FROM emailresult
            WHERE created_at >= NOW() - INTERVAL '13 days'
            GROUP BY d, verdict ORDER BY d
        """
    return """
        SELECT strftime('%Y-%m-%d', created_at) AS d, verdict, COUNT(*) AS cnt
        FROM emailresult
        WHERE created_at >= date('now', '-13 days')
        GROUP BY d, verdict ORDER BY d
    """


def _domain_sql() -> str:
    if is_postgres():
        return """
            SELECT SPLIT_PART(email, '@', 2) AS domain, COUNT(*) AS cnt
            FROM emailresult WHERE verdict = 'invalid'
            GROUP BY domain ORDER BY cnt DESC LIMIT 10
        """
    return """
        SELECT substr(email, instr(email, '@') + 1) AS domain, COUNT(*) AS cnt
        FROM emailresult WHERE verdict = 'invalid'
        GROUP BY domain ORDER BY cnt DESC LIMIT 10
    """


@router.get("/api/stats")
def get_stats(current_user: User = Depends(require_auth)):
    with Session(engine) as session:
        total_results = session.exec(select(func.count()).select_from(EmailResult)).one() or 0
        total_cache = session.exec(select(func.count()).select_from(EmailCache)).one() or 0

        verdict_rows = session.execute(text(
            "SELECT verdict, COUNT(*) FROM emailresult GROUP BY verdict"
        )).fetchall()
        verdict_counts = {r[0]: r[1] for r in verdict_rows}

        daily_rows = session.execute(text(_daily_sql())).fetchall()
        daily: dict[str, dict[str, int]] = defaultdict(
            lambda: {"valid": 0, "invalid": 0, "risky": 0, "unknown": 0}
        )
        for date_str, verdict, cnt in daily_rows:
            daily[date_str][verdict] = cnt

        domain_rows = session.execute(text(_domain_sql())).fetchall()

    cache_rate = round(total_cache / total_results * 100, 1) if total_results > 0 else 0
    return {
        "total_validated": total_results,
        "total_cached": total_cache,
        "cache_hit_rate": cache_rate,
        "verdict_counts": verdict_counts,
        "daily_stats": [{"date": d, **counts} for d, counts in sorted(daily.items())],
        "top_invalid_domains": [{"domain": r[0], "count": r[1]} for r in domain_rows],
    }


def _require_admin_cache(current_user: User) -> None:
    """EmailCache is shared across all users — mutations and bulk reads
    (export/purge/delete) must stay admin-only."""
    if current_user.role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin only")


@router.post("/api/cache/purge")
def purge_cache(current_user: User = Depends(require_auth)):
    _require_admin_cache(current_user)
    count = purge_expired()
    return {"purged": count}


_VALID_EXPORT_VERDICTS = {"valid", "invalid", "risky"}


@router.get("/api/cache/export")
def export_cache(
    q: str = "",
    verdict: str = "",
    current_user: User = Depends(require_auth),
):
    """Export the cache table as CSV. Honors the same `q` + `verdict`
    filters as the browser.

    Column projection drops `provider_data` (a fat JSON blob that
    isn't part of the CSV anyway) so the DB only fetches the 6 fields
    we actually write — that's what keeps the 50k-row export under
    Vercel's 10s ceiling. Previous attempt at StreamingResponse +
    yield_per returned a header-only blank file on Vercel's ASGI
    runtime; non-streaming + projection is the safer shape.

    For exports too large for the 10s budget, use the
    .github/workflows/export_cache.yml workflow — runs on GHA with no
    timeout and uploads the CSV as an artifact."""
    _require_admin_cache(current_user)
    verdict_q = verdict.strip().lower() if verdict.strip().lower() in _VALID_EXPORT_VERDICTS else ""

    # Column-projected query — never loads EmailCache.provider_data.
    stmt = (
        select(
            EmailCache.email, EmailCache.verdict, EmailCache.providers_used,
            EmailCache.strategy, EmailCache.validated_at, EmailCache.expires_at,
        )
        .order_by(EmailCache.validated_at.desc())  # type: ignore[arg-type]
    )
    if q:
        stmt = stmt.where(EmailCache.email.contains(q))
    if verdict_q:
        stmt = stmt.where(EmailCache.verdict == verdict_q)
    with Session(engine) as session:
        rows = session.execute(stmt).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "email", "verdict", "providers_used", "strategy",
        "validated_at", "expires_at",
    ])
    for email, vd, providers_used, strategy, validated_at, expires_at in rows:
        writer.writerow([
            email or "",
            vd or "",
            providers_used or "",
            strategy or "",
            validated_at.isoformat() if validated_at else "",
            expires_at.isoformat() if expires_at else "",
        ])

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    filename = f"email-cache-{stamp}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/api/cache/{cache_id}")
def delete_cache_entry(cache_id: int, current_user: User = Depends(require_auth)):
    _require_admin_cache(current_user)
    with Session(engine) as session:
        row = session.get(EmailCache, cache_id)
        if not row:
            raise HTTPException(status_code=404, detail="Cache entry not found")
        session.delete(row)
        session.commit()
    return {"deleted": True}


@router.post("/api/cache/clear")
def clear_all_cache(current_user: User = Depends(require_auth)):
    """Delete every cache row. Admin-only — wipes shared cache for all users."""
    if current_user.role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin only")
    with Session(engine) as session:
        count = session.execute(text("DELETE FROM emailcache")).rowcount or 0
        session.commit()
    return {"deleted": count}


@router.get("/api/domain/{domain}")
def get_domain_reputation(domain: str, current_user: User = Depends(require_auth)):
    with Session(engine) as session:
        rows = session.execute(text("""
            SELECT verdict, COUNT(*) AS cnt FROM emailcache
            WHERE email LIKE :pattern GROUP BY verdict
        """), {"pattern": f"%@{domain.lower()}"}).fetchall()

    verdict_counts = {r[0]: r[1] for r in rows}
    total = sum(verdict_counts.values())
    if total == 0:
        reputation = "unknown"
    elif verdict_counts.get("invalid", 0) / total > 0.5:
        reputation = "bad"
    elif verdict_counts.get("valid", 0) / total > 0.7:
        reputation = "good"
    else:
        reputation = "mixed"

    return {
        "domain": domain,
        "total_checked": total,
        "verdict_counts": verdict_counts,
        "reputation": reputation,
    }
