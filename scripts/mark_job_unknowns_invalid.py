"""Flip every 'unknown' EmailResult row for a given job_id to 'invalid'.

Use when you want to skip the remaining retry strikes for a job — the
unknowns get force-marked as invalid so they leave the retry pool and
the job's verdict distribution is fully resolved.

Usage:
    DATABASE_URL=postgres://... python scripts/mark_job_unknowns_invalid.py --job-id 79
    DATABASE_URL=postgres://... python scripts/mark_job_unknowns_invalid.py --job-id 79 --dry-run
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db import engine  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    with Session(engine) as db:
        n = db.execute(
            text("SELECT COUNT(*) FROM emailresult "
                 "WHERE job_id = :jid AND verdict = 'unknown'"),
            {"jid": args.job_id},
        ).scalar() or 0
        print(f"job {args.job_id}: {n} unknown rows")
        if args.dry_run or n == 0:
            print("[dry-run]" if args.dry_run else "nothing to do")
            return 0
        rows = db.execute(
            text("UPDATE emailresult SET verdict = 'invalid' "
                 "WHERE job_id = :jid AND verdict = 'unknown'"),
            {"jid": args.job_id},
        ).rowcount or 0
        db.commit()
        print(f"flipped {rows} rows from unknown -> invalid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
