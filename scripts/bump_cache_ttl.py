"""One-off: extend every EmailCache row's expires_at out to N days from
its `validated_at` timestamp.

When the default `cache_ttl_days` was bumped 30 → 365 in PR #32, only
*new* validations got the longer TTL. Existing rows still carry the
30-day `expires_at` they were originally written with — they'll expire
in the next month even though the policy now says they should live a
year. This script back-fills them.

Idempotent: WHERE clause skips rows whose `expires_at` is already at
least `:target_days` past `validated_at`, so re-running is a no-op.

Usage:
    DATABASE_URL=postgres://... python scripts/bump_cache_ttl.py
    DATABASE_URL=postgres://... python scripts/bump_cache_ttl.py --days 365
    DATABASE_URL=postgres://... python scripts/bump_cache_ttl.py --dry-run
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db import engine, is_postgres  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=365,
                   help="Target TTL in days from validated_at (default: 365).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the affected row count without updating.")
    args = p.parse_args()

    if args.days < 1:
        print("--days must be >= 1", flush=True)
        return 1

    # Postgres has INTERVAL arithmetic; SQLite needs different date math.
    # This script targets prod (Postgres). Local SQLite dev rarely needs it.
    if is_postgres():
        count_sql = text(
            "SELECT COUNT(*) FROM emailcache "
            "WHERE expires_at < validated_at + (:d || ' days')::INTERVAL"
        )
        update_sql = text(
            "UPDATE emailcache "
            "SET expires_at = validated_at + (:d || ' days')::INTERVAL "
            "WHERE expires_at < validated_at + (:d || ' days')::INTERVAL"
        )
    else:
        count_sql = text(
            "SELECT COUNT(*) FROM emailcache "
            "WHERE expires_at < datetime(validated_at, '+' || :d || ' days')"
        )
        update_sql = text(
            "UPDATE emailcache "
            "SET expires_at = datetime(validated_at, '+' || :d || ' days') "
            "WHERE expires_at < datetime(validated_at, '+' || :d || ' days')"
        )

    with Session(engine) as db:
        n = db.execute(count_sql, {"d": args.days}).scalar() or 0
        print(f"Rows with expires_at < validated_at + {args.days} days: {n}", flush=True)
        if args.dry_run:
            print("[dry-run] nothing updated.", flush=True)
            return 0
        if n == 0:
            print("Nothing to do — every row already at or beyond target TTL.", flush=True)
            return 0
        rows = db.execute(update_sql, {"d": args.days}).rowcount or 0
        db.commit()
        print(f"Bumped {rows} rows to expires_at = validated_at + {args.days} days.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
