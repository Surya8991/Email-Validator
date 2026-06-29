"""Mark jobs stuck in 'running' as failed.

A bulk_process GitHub Actions run has a 360-minute (6h) timeout. If the
runner is killed or the workflow is cancelled before the callback fires,
the Job row stays 'running' forever. This script detects those and marks
them failed so the UI doesn't show them as in-flight indefinitely.

Threshold: jobs still 'running' after 7 hours (1h buffer beyond the
workflow timeout) are considered stale.

Usage:
    python scripts/fail_stale_jobs.py
    python scripts/fail_stale_jobs.py --hours 7   # custom threshold
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from sqlmodel import Session

from app.db import engine


def fail_stale(hours: int) -> int:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with Session(engine) as db:
        rows = db.execute(
            text("SELECT id, created_at FROM job WHERE status = 'running' AND created_at < :cutoff"),
            {"cutoff": cutoff},
        ).fetchall()

        if not rows:
            print(f"[stale-jobs] no stale jobs found (threshold: {hours}h)")
            return 0

        ids = [r[0] for r in rows]
        print(f"[stale-jobs] found {len(ids)} stale job(s): {ids}")
        db.execute(
            text(
                "UPDATE job SET status = 'failed', error = :err "
                "WHERE id = ANY(:ids)"
            ),
            {
                "err": f"Job timed out — still running after {hours}h. "
                       "Worker was likely killed. Re-queue to retry.",
                "ids": ids,
            },
        )
        db.commit()
        print(f"[stale-jobs] marked {len(ids)} job(s) as failed.")
        return len(ids)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=7,
                   help="Mark jobs still running after this many hours as failed (default: 7)")
    args = p.parse_args()
    fail_stale(args.hours)
