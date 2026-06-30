"""Run the LocalProvider on remaining 'unknown' emails and flip the bad ones to 'invalid'.

The local check is free (syntax + MX/A DNS lookup, no API credit). Anything
local says is `invalid` (bad syntax or no DNS) can't ever deliver, so it's
safe to flip without burning Bouncify credits.

By default ONLY local 'invalid' results get flipped. `--aggressive` also
flips local 'risky' (disposable domain) results.

Usage:
    DATABASE_URL=... python scripts/local_triage_unknowns.py --dry-run
    DATABASE_URL=... python scripts/local_triage_unknowns.py
    DATABASE_URL=... python scripts/local_triage_unknowns.py --aggressive
    DATABASE_URL=... python scripts/local_triage_unknowns.py --min-retry-count 1

Concurrency = 20 by default (CHUNK_SIZE env var to override). All DNS work is
done in background threads so we don't block the event loop.

Exit code 0 on full success.
"""
import argparse
import asyncio
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.providers.local import LocalProvider  # noqa: E402

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "20"))
PROGRESS_EVERY = int(os.getenv("PROGRESS_EVERY", "200"))


def _fetch_unknown_emails(min_rc: int) -> list[str]:
    with Session(engine) as db:
        rows = db.execute(text(
            "SELECT DISTINCT email FROM emailresult "
            "WHERE verdict = 'unknown' AND retry_count >= :rc "
            "ORDER BY email"
        ), {"rc": min_rc}).fetchall()
    return [r[0] for r in rows if r[0]]


def _flip_email(email: str) -> int:
    with Session(engine) as db:
        n = db.execute(text(
            "UPDATE emailresult SET verdict = 'invalid' "
            "WHERE email = :em AND verdict = 'unknown'"
        ), {"em": email}).rowcount or 0
        db.commit()
    return n


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Print the distribution without flipping anything.")
    p.add_argument("--aggressive", action="store_true",
                   help="Also flip local 'risky' (e.g. disposable) results.")
    p.add_argument("--min-retry-count", type=int, default=0,
                   help="Only process unknowns with retry_count >= this. Default 0.")
    args = p.parse_args()

    emails = _fetch_unknown_emails(args.min_retry_count)
    total = len(emails)
    print(f"unknowns to triage: {total} (retry_count >= {args.min_retry_count})")
    if total == 0:
        return 0

    provider = LocalProvider()
    counts: Counter = Counter()
    rows_flipped = 0
    started = time.monotonic()
    flip_statuses = {"invalid"} | ({"risky"} if args.aggressive else set())

    for i in range(0, total, CHUNK_SIZE):
        slice_ = emails[i : i + CHUNK_SIZE]
        results = await asyncio.gather(
            *[provider.verify(e) for e in slice_], return_exceptions=True
        )
        for em, res in zip(slice_, results):
            if isinstance(res, Exception):
                counts["error"] += 1
                continue
            counts[res.status] += 1
            if res.status in flip_statuses and not args.dry_run:
                rows_flipped += _flip_email(em)
        done = i + len(slice_)
        if done % PROGRESS_EVERY < CHUNK_SIZE or done == total:
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed else 0
            print(
                f"  {done}/{total} | valid={counts['valid']} "
                f"invalid={counts['invalid']} risky={counts['risky']} "
                f"err={counts['error']} | {rate:.1f} emails/s",
                flush=True,
            )

    print()
    print("=== local triage summary ===")
    for k, v in sorted(counts.items()):
        print(f"  {k:<10} {v}")
    print(f"Flip-eligible (local status in {sorted(flip_statuses)}): "
          f"{sum(counts[s] for s in flip_statuses)}")
    if args.dry_run:
        print("[dry-run] no rows updated.")
    else:
        print(f"emailresult rows flipped unknown -> invalid: {rows_flipped}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
