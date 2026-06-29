"""Re-validate emails that previously returned 'unknown'.

Background: unknowns mean the provider didn't yield a determination
(timeout, 5xx, parser drift, rate-limit). They're not cached
(validator.py skips set_cache for 'unknown'), so every retry actually
hits the provider again — that's the point.

Usage:
    python scripts/retry_unknowns.py --batch-size 500
    python scripts/retry_unknowns.py --batch-size 500 --max-batches 4
    python scripts/retry_unknowns.py --job-id 27         # only that job's unknowns
    python scripts/retry_unknowns.py --since-days 7      # only recent unknowns

Each batch:
  1. SELECT DISTINCT email FROM emailresult WHERE verdict='unknown' LIMIT N
  2. Re-validate via the same provider waterfall (cache-checked first; a
     hit means another worker already resolved it since the row was
     written, so we don't burn another credit).
  3. UPDATE every emailresult row for that email with the new verdict +
     provider_data. Cache is written by the validator on success.

Exit code: 0 on full success, 2 if any batch errored.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.cache import get_cached, parse_cached_providers, set_cache  # noqa: E402
from app.core.validator import validate  # noqa: E402
from app.db import create_db_tables, engine  # noqa: E402
from app.providers import registry  # noqa: E402


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


CHUNK_SIZE = _env_int("CHUNK_SIZE", 20)


def _unknown_emails(
    session: Session,
    batch_size: int,
    *,
    job_id: int | None,
    since: datetime | None,
    exclude: set[str] | None = None,
) -> list[str]:
    clauses = ["verdict = 'unknown'"]
    params: dict = {"limit": batch_size}
    if job_id is not None:
        clauses.append("job_id = :jid")
        params["jid"] = job_id
    if since is not None:
        clauses.append("created_at >= :since")
        params["since"] = since
    if exclude:
        # Bind as a tuple expanded via SQLAlchemy's expanding param so the
        # set can be tens of thousands of emails without one big string.
        from sqlalchemy import bindparam

        stmt = text(
            f"SELECT DISTINCT email FROM emailresult WHERE {' AND '.join(clauses)} "
            f"AND email NOT IN :excl ORDER BY email LIMIT :limit"
        ).bindparams(bindparam("excl", expanding=True))
        params["excl"] = list(exclude)
        return [r[0] for r in session.execute(stmt, params).fetchall() if r[0]]
    where = " AND ".join(clauses)
    sql = (
        f"SELECT DISTINCT email FROM emailresult WHERE {where} "
        f"ORDER BY email LIMIT :limit"
    )
    return [r[0] for r in session.execute(text(sql), params).fetchall() if r[0]]


def _update_rows(
    session: Session,
    email: str,
    verdict: str,
    provider_data_json: str,
    *,
    job_id: int | None,
) -> int:
    clauses = ["email = :email", "verdict = 'unknown'"]
    params: dict = {
        "email": email,
        "verdict_new": verdict,
        "pdata": provider_data_json,
    }
    if job_id is not None:
        clauses.append("job_id = :jid")
        params["jid"] = job_id
    where = " AND ".join(clauses)
    sql = (
        f"UPDATE emailresult SET verdict = :verdict_new, provider_data = :pdata "
        f"WHERE {where}"
    )
    return session.execute(text(sql), params).rowcount or 0


async def _validate_one(
    email: str,
    providers: list[str],
    strategy: str,
) -> tuple[str, dict]:
    cached = get_cached(email)
    if cached:
        parsed = parse_cached_providers(cached)
        return cached.verdict, {n: r.model_dump() for n, r in parsed.items()}
    verdict, provider_results = await validate(email, providers, strategy)
    if verdict != "unknown":
        set_cache(email, verdict, provider_results, strategy)
    return verdict, {n: r.model_dump() for n, r in provider_results.items()}


async def _process_batch(
    emails: list[str],
    providers: list[str],
    strategy: str,
    *,
    job_id: int | None,
) -> dict[str, int]:
    stats = {"resolved": 0, "still_unknown": 0, "rows_updated": 0, "errors": 0}
    for i in range(0, len(emails), CHUNK_SIZE):
        slice_ = emails[i : i + CHUNK_SIZE]
        tasks = [_validate_one(em, providers, strategy) for em in slice_]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        with Session(engine) as session:
            for em, res in zip(slice_, results):
                if isinstance(res, Exception):
                    stats["errors"] += 1
                    print(f"  [WARN] {em}: {type(res).__name__}: {res}", flush=True)
                    continue
                verdict, pdata = res
                if verdict == "unknown":
                    stats["still_unknown"] += 1
                    continue
                rows = _update_rows(
                    session, em, verdict, json.dumps(pdata), job_id=job_id,
                )
                stats["resolved"] += 1
                stats["rows_updated"] += rows
            session.commit()
    return stats


async def run(args: argparse.Namespace) -> int:
    create_db_tables()
    registry._client = httpx.AsyncClient(timeout=settings.httpx_timeout)

    providers = [p.strip() for p in (args.providers or "bouncify").split(",") if p.strip()]
    strategy = args.strategy or "bouncify_only"
    since = (
        datetime.utcnow() - timedelta(days=args.since_days)
        if args.since_days and args.since_days > 0
        else None
    )

    total = {"resolved": 0, "still_unknown": 0, "rows_updated": 0, "errors": 0, "batches": 0}
    # Without this, a batch whose every email comes back 'unknown' again
    # (Bouncify still timing out on them) would refetch the same 500 next
    # round — ORDER BY email LIMIT 500 doesn't move on. Exclude
    # already-attempted emails from subsequent queries this run.
    attempted: set[str] = set()
    try:
        for batch_no in range(1, (args.max_batches or 10_000) + 1):
            with Session(engine) as session:
                emails = _unknown_emails(
                    session,
                    args.batch_size,
                    job_id=args.job_id,
                    since=since,
                    exclude=attempted,
                )
            if not emails:
                print("No unknown emails left to retry.", flush=True)
                break
            attempted.update(emails)
            print(
                f"[batch {batch_no}] {len(emails)} unknowns | "
                f"providers={providers} | strategy={strategy} | "
                f"attempted-so-far={len(attempted)}",
                flush=True,
            )
            stats = await _process_batch(
                emails, providers, strategy, job_id=args.job_id,
            )
            total["batches"] += 1
            for k in ("resolved", "still_unknown", "rows_updated", "errors"):
                total[k] += stats[k]
            # Safety net for total bouncify outage: if a whole batch
            # produced zero resolutions, the provider is no help right
            # now — stop burning credits.
            all_still_unknown = stats["still_unknown"] == len(emails)
            if stats["resolved"] == 0 and stats["errors"] == 0 and all_still_unknown:
                print(
                    "  → 0 resolved this batch — provider not helping, stopping early.",
                    flush=True,
                )
                break
            print(
                f"  → resolved={stats['resolved']} "
                f"still_unknown={stats['still_unknown']} "
                f"rows_updated={stats['rows_updated']} "
                f"errors={stats['errors']}",
                flush=True,
            )
    finally:
        await registry._client.aclose()

    print("\n=== summary ===", flush=True)
    for k, v in total.items():
        print(f"  {k}: {v}", flush=True)
    return 2 if total["errors"] > 0 else 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--batch-size", type=int, default=500,
                   help="Unknown emails fetched per DB round-trip (default: 500).")
    p.add_argument("--max-batches", type=int, default=0,
                   help="Stop after N batches (default: 0 = run until no unknowns left).")
    p.add_argument("--job-id", type=int, default=None,
                   help="Restrict to unknowns in this job (default: all jobs).")
    p.add_argument("--since-days", type=int, default=0,
                   help="Only retry unknowns created in the last N days (default: all time).")
    p.add_argument("--providers", default=None,
                   help="Comma-separated provider list (default: bouncify).")
    p.add_argument("--strategy", default=None,
                   help="Validation strategy (default: bouncify_only).")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(_parse_args())))
