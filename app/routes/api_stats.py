from collections import defaultdict

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.core.cache import purge_expired
from app.db import engine, is_postgres
from app.models import EmailCache, EmailResult

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
def get_stats():
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


@router.post("/api/cache/purge")
def purge_cache():
    count = purge_expired()
    return {"purged": count}


@router.delete("/api/cache/{cache_id}")
def delete_cache_entry(cache_id: int):
    with Session(engine) as session:
        row = session.get(EmailCache, cache_id)
        if not row:
            raise HTTPException(status_code=404, detail="Cache entry not found")
        session.delete(row)
        session.commit()
    return {"deleted": True}


@router.get("/api/domain/{domain}")
def get_domain_reputation(domain: str):
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
