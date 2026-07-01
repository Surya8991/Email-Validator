"""One-off backfill: sync EmailCache from EmailResult's resolved verdicts.

Background: retry_unknowns.py's strike-out path flipped EmailResult rows
from 'unknown' to 'invalid' via a raw UPDATE without ever touching
EmailCache (fixed going forward — see bulk_set_cache_invalid() calls in
retry_unknowns.py). This script repairs the backlog that piled up before
that fix landed: it finds every email whose latest resolved (valid /
invalid / risky) EmailResult row disagrees with — or is missing from —
EmailCache, and bulk-upserts the correct verdict + provider_data.

Safe to re-run: only touches emails where EmailResult and EmailCache
actually disagree, so a second run after the gap is closed is a no-op.

Usage:
    python scripts/reconcile_email_cache.py --dry-run
    python scripts/reconcile_email_cache.py --batch-size 2000
"""
import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.cache import bulk_upsert_cache_rows, get_cached_many  # noqa: E402
from app.db import engine  # noqa: E402

# ROW_NUMBER() OVER (PARTITION BY ...) works identically on Postgres and
# SQLite 3.25+ (the version bundled with Python 3.11+), so no dialect
# branch is needed here — unlike the ON CONFLICT upsert syntax.
_LATEST_RESOLVED_SQL = """
    SELECT email, verdict, provider_data FROM (
        SELECT email, verdict, provider_data,
               ROW_NUMBER() OVER (
                   PARTITION BY email ORDER BY created_at DESC, id DESC
               ) AS rn
        FROM emailresult
        WHERE verdict IN ('valid', 'invalid', 'risky')
    ) t
    WHERE rn = 1
    ORDER BY email
    LIMIT :limit OFFSET :offset
"""


def _fetch_batch(session: Session, batch_size: int, offset: int) -> list[tuple[str, str, str]]:
    rows = session.execute(
        text(_LATEST_RESOLVED_SQL), {"limit": batch_size, "offset": offset}
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def run(batch_size: int, dry_run: bool) -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    expires = now + timedelta(days=settings.cache_ttl_days)

    offset = 0
    scanned = 0
    mismatched = 0
    synced = 0

    with Session(engine) as session:
        while True:
            batch = _fetch_batch(session, batch_size, offset)
            if not batch:
                break
            offset += len(batch)
            scanned += len(batch)

            emails = [email for email, _, _ in batch]
            cached_map = get_cached_many(emails)

            to_sync = []
            for email, verdict, provider_data in batch:
                key = email.strip().lower()
                cached = cached_map.get(key)
                if cached is not None and cached.verdict == verdict:
                    continue
                mismatched += 1
                to_sync.append({
                    "email": key,
                    "verdict": verdict,
                    "provider_data": provider_data or "{}",
                    "providers_used": "reconcile_backfill",
                    "strategy": "reconcile_backfill",
                    "validated_at": now,
                    "expires_at": expires,
                })

            if to_sync and not dry_run:
                synced += bulk_upsert_cache_rows(to_sync)

            print(
                f"  scanned={scanned} mismatched-so-far={mismatched} "
                f"synced-so-far={synced}",
                flush=True,
            )

    print("\n=== summary ===", flush=True)
    print(f"  resolved emails scanned: {scanned}", flush=True)
    print(f"  missing/mismatched vs EmailCache: {mismatched}", flush=True)
    if dry_run:
        print("  [dry-run] no writes performed", flush=True)
    else:
        print(f"  synced to EmailCache: {synced}", flush=True)
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--batch-size", type=int, default=2000,
                   help="Resolved EmailResult rows scanned per round-trip (default: 2000).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print counts without writing to EmailCache.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(run(args.batch_size, args.dry_run))
