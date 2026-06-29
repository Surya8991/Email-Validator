"""
Delete a job and its EmailResult rows from the database.

Usage:
    DATABASE_URL=postgres://... python scripts/delete_job.py --job-id 10
    DATABASE_URL=postgres://... python scripts/delete_job.py --job-id 10 --dry-run
    DATABASE_URL=postgres://... python scripts/delete_job.py --job-id 10 --yes

Without --yes you'll be asked for an interactive y/N confirmation.
Use --dry-run to preview row counts without deleting anything.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db import engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete a Job and its EmailResult rows")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no delete")
    parser.add_argument("--yes", action="store_true", help="Skip the y/N prompt")
    args = parser.parse_args()

    with Session(engine) as session:
        job_row = session.execute(
            text("SELECT id, status, total, processed, filename FROM job WHERE id = :jid"),
            {"jid": args.job_id},
        ).fetchone()
        if not job_row:
            print(f"Job {args.job_id} not found.")
            return 1

        result_count = session.execute(
            text("SELECT COUNT(*) FROM emailresult WHERE job_id = :jid"),
            {"jid": args.job_id},
        ).scalar() or 0

        print(f"Job {args.job_id}:")
        print(f"  status={job_row[1]}  total={job_row[2]}  processed={job_row[3]}")
        print(f"  filename={job_row[4]}")
        print(f"  EmailResult rows: {result_count}")

        if args.dry_run:
            print("[dry-run] Nothing deleted.")
            return 0

        if job_row[1] == "running":
            print("ERROR: refusing to delete a running job (worker would crash).")
            print("Wait for it to finish/fail, or manually mark status='failed' first.")
            return 2

        if not args.yes:
            confirm = input(
                f"Delete job {args.job_id} and {result_count} result rows? [y/N] "
            ).strip().lower()
            if confirm not in ("y", "yes"):
                print("Aborted.")
                return 0

        session.execute(
            text("DELETE FROM emailresult WHERE job_id = :jid"),
            {"jid": args.job_id},
        )
        session.execute(text("DELETE FROM job WHERE id = :jid"), {"jid": args.job_id})
        session.commit()
        print(f"Deleted job {args.job_id} and {result_count} EmailResult rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
