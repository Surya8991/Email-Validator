"""Report verdict x retry_count distribution across EmailResult.

Used to verify the 2-strikes invariant: every row with retry_count >= STRIKES
should have verdict='invalid' (never 'unknown'). If any row violates that,
the count is printed loud so we can fix it.

Usage:
    DATABASE_URL=postgres://... python scripts/audit_unknowns.py
    DATABASE_URL=postgres://... python scripts/audit_unknowns.py --strikes 2
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
    p.add_argument("--strikes", type=int, default=2,
                   help="Strikes threshold. Rows with retry_count >= strikes "
                        "MUST have verdict='invalid' (default: 2)")
    args = p.parse_args()

    with Session(engine) as db:
        print("=== verdict x retry_count distribution ===")
        rows = db.execute(text(
            "SELECT verdict, retry_count, COUNT(*) "
            "FROM emailresult GROUP BY verdict, retry_count "
            "ORDER BY verdict, retry_count"
        )).fetchall()
        for v, rc, n in rows:
            print(f"  verdict={v!r:<12} retry_count={rc} -> {n}")

        print()
        stuck = db.execute(text(
            "SELECT COUNT(*) FROM emailresult "
            "WHERE retry_count >= :s AND verdict = 'unknown'"
        ), {"s": args.strikes}).scalar() or 0
        if stuck > 0:
            print(f"*** WARNING: {stuck} rows have retry_count >= {args.strikes} "
                  f"but still verdict='unknown'. The 2-strikes invariant is violated.")
            print(f"    Fix with: UPDATE emailresult SET verdict='invalid' "
                  f"WHERE retry_count >= {args.strikes} AND verdict='unknown';")
            return 2
        print(f"OK: no rows violate the strikes={args.strikes} invariant.")

        total_unknown = db.execute(text(
            "SELECT COUNT(*) FROM emailresult WHERE verdict = 'unknown'"
        )).scalar() or 0
        print(f"Total unknown rows remaining: {total_unknown}")
        if total_unknown > 0:
            print("  (these will be retried by the scheduled cron until "
                  f"retry_count >= {args.strikes}, then auto-flipped to invalid)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
