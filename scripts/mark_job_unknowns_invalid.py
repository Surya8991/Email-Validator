"""Flip 'unknown' EmailResult rows to 'invalid'.

Use to skip retry strikes — the rows get force-marked as invalid so they
leave the retry pool and the verdict distribution becomes fully resolved.

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func  # noqa: E402
from sqlalchemy import update as sa_update
from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import EmailResult  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--job-id", type=int, default=None,
                   help="Restrict to one job (omit for all jobs).")
    p.add_argument("--min-retry-count", type=int, default=0,
                   help="Only flip rows with retry_count >= this value. Default 0 (all).")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    # Build the WHERE via SQLAlchemy column expressions rather than
    # text(f"...{where}") interpolation — keeps CodeQL's py/sql-injection
    # taint analyzer happy and isn't tied to the table being literal SQL.
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
        rows = db.execute(
            _scope_filter(sa_update(EmailResult)).values(verdict="invalid")
        ).rowcount or 0
        db.commit()
        print(f"flipped {rows} rows from unknown -> invalid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
