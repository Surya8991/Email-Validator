"""Flip 'unknown' EmailResult rows to 'invalid' and sync EmailCache.

Use to skip retry strikes — the rows get force-marked as invalid so they
leave the retry pool and the verdict distribution becomes fully resolved.
Also upserts each flipped email into EmailCache so the Account Cleanup
cache-breakdown reflects the true state (without this, flipped rows stay
as "not in cache (KEEP)" instead of "invalid (DROP)").

Scope filters (combine as needed):
- --job-id N             restrict to one job
- --min-retry-count N    only rows with retry_count >= N (default 0 = all)
- --dry-run              print the count without updating

Examples:
    # Flip all unknowns for one job
    python scripts/mark_job_unknowns_invalid.py --job-id 79

    # Flip every unknown at retry_count >= 2 across the whole DB
    python scripts/mark_job_unknowns_invalid.py --min-retry-count 2

    # Preview without changing
    python scripts/mark_job_unknowns_invalid.py --min-retry-count 2 --dry-run
"""
import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func  # noqa: E402
from sqlalchemy import update as sa_update
from sqlmodel import Session, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import engine, is_postgres  # noqa: E402
from app.models import EmailCache, EmailResult  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--job-id", type=int, default=None,
                   help="Restrict to one job (omit for all jobs).")
    p.add_argument("--min-retry-count", type=int, default=0,
                   help="Only flip rows with retry_count >= this value. Default 0 (all).")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    def _scope_filter(stmt):
        stmt = stmt.where(EmailResult.verdict == "unknown")
        if args.job_id is not None:
            stmt = stmt.where(EmailResult.job_id == args.job_id)
        if args.min_retry_count > 0:
            stmt = stmt.where(EmailResult.retry_count >= args.min_retry_count)
        return stmt

    scope = f"job_id={args.job_id}" if args.job_id else "all jobs"
    if args.min_retry_count > 0:
        scope += f", retry_count >= {args.min_retry_count}"

    with Session(engine) as db:
        n = db.execute(
            _scope_filter(select(func.count()).select_from(EmailResult))
        ).scalar() or 0
        print(f"scope: {scope} -> {n} unknown rows")
        if args.dry_run or n == 0:
            print("[dry-run]" if args.dry_run else "nothing to do")
            return 0

        # Fetch emails before flipping so we can sync the cache.
        emails_to_flip = db.execute(
            _scope_filter(select(EmailResult.email))
        ).scalars().all()

        rows = db.execute(
            _scope_filter(sa_update(EmailResult)).values(verdict="invalid")
        ).rowcount or 0
        db.commit()
        print(f"flipped {rows} rows from unknown -> invalid")

    # Sync EmailCache so the Account Cleanup cache-breakdown shows these as
    # "invalid (DROP)" rather than "not in cache (KEEP)".
    _sync_cache(emails_to_flip)
    return 0


def _sync_cache(emails: list[str]) -> None:
    """Bulk-upsert all flipped emails into EmailCache as 'invalid'.

    Uses a single INSERT ... ON CONFLICT DO UPDATE per 1000-row chunk so
    the whole sync takes one round-trip per chunk instead of one per email.
    """
    if not emails:
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    expires = now + timedelta(days=settings.cache_ttl_days)
    provider_data = json.dumps({"force_flip": {"status": "invalid", "sub_status": None,
                                                "raw": None, "confidence": None}})
    chunk_size = 1000
    synced = 0

    for i in range(0, len(emails), chunk_size):
        chunk = [e.strip().lower() for e in emails[i : i + chunk_size] if e and e.strip()]
        if not chunk:
            continue
        rows = [
            {
                "email": email,
                "verdict": "invalid",
                "provider_data": provider_data,
                "providers_used": "force_flip",
                "strategy": "force_flip",
                "validated_at": now,
                "expires_at": expires,
            }
            for email in chunk
        ]
        with Session(engine) as db:
            if is_postgres():
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                stmt = pg_insert(EmailCache).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["email"],
                    set_={
                        "verdict": stmt.excluded.verdict,
                        "provider_data": stmt.excluded.provider_data,
                        "providers_used": stmt.excluded.providers_used,
                        "strategy": stmt.excluded.strategy,
                        "validated_at": stmt.excluded.validated_at,
                        "expires_at": stmt.excluded.expires_at,
                    },
                )
            else:
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                stmt = sqlite_insert(EmailCache).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["email"],
                    set_={
                        "verdict": stmt.excluded.verdict,
                        "provider_data": stmt.excluded.provider_data,
                        "providers_used": stmt.excluded.providers_used,
                        "strategy": stmt.excluded.strategy,
                        "validated_at": stmt.excluded.validated_at,
                        "expires_at": stmt.excluded.expires_at,
                    },
                )
            db.execute(stmt)
            db.commit()
        synced += len(chunk)
        print(f"  cache sync {synced}/{len(emails)}")
    print(f"synced {synced} emails to EmailCache as invalid")


if __name__ == "__main__":
    sys.exit(main())
