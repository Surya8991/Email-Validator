"""Read-only report: platform-wide EmailResult verdicts vs live EmailCache
verdicts, to confirm the two stay proportionally consistent.

Background: Session 26 fixed the retry_unknowns.py cache-sync bug and
backfilled the existing drift (see PROJECT_LOG.md). This script prints
both distributions side by side so that check can be repeated without
opening the UI. Read-only — no writes.

Usage:
    python scripts/report_verdict_reconciliation.py
"""
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db import engine  # noqa: E402


def main() -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as db:
        result_rows = db.execute(text(
            "SELECT verdict, COUNT(*) FROM emailresult GROUP BY verdict"
        )).fetchall()
        cache_rows = db.execute(text(
            "SELECT verdict, COUNT(*) FROM emailcache "
            "WHERE expires_at > :now GROUP BY verdict"
        ), {"now": now}).fetchall()

    result_counts = {v: n for v, n in result_rows}
    cache_counts = {v: n for v, n in cache_rows}
    result_total = sum(result_counts.values())
    cache_total = sum(cache_counts.values())

    print("=== EmailResult (all-time, platform-wide) ===")
    for v in ("valid", "invalid", "risky", "unknown"):
        n = result_counts.get(v, 0)
        pct = (n / result_total * 100) if result_total else 0
        print(f"  {v:<8} {n:>8}  ({pct:5.1f}%)")
    print(f"  {'TOTAL':<8} {result_total:>8}")

    print("\n=== EmailCache (live, non-expired) ===")
    for v in ("valid", "invalid", "risky", "unknown"):
        n = cache_counts.get(v, 0)
        pct = (n / cache_total * 100) if cache_total else 0
        print(f"  {v:<8} {n:>8}  ({pct:5.1f}%)")
    print(f"  {'TOTAL':<8} {cache_total:>8}")

    print("\n=== Ratio (EmailResult / EmailCache) — should be similar across verdicts ===")
    for v in ("valid", "invalid", "risky"):
        r, c = result_counts.get(v, 0), cache_counts.get(v, 0)
        ratio = (r / c) if c else float("inf")
        print(f"  {v:<8} {r:>8} / {c:<8} = {ratio:.2f}x")

    return 0


if __name__ == "__main__":
    sys.exit(main())
