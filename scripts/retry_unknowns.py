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
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.cache import (  # noqa: E402
    bulk_set_cache_invalid,
    get_cached,
    parse_cached_providers,
    set_cache,
)
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


# Default 5 (lower than bulk_process's 20) because retry runs single-worker
# against emails Bouncify already failed on — heavier per-call latency,
# higher 429 risk. Bump via CHUNK_SIZE repo variable if you see steady
# headroom in the progress log.
CHUNK_SIZE = _env_int("CHUNK_SIZE", 5)
# Log a progress line every this many emails inside a batch so GHA shows
# live throughput instead of going dark for ~10 min per 500-email batch.
PROGRESS_EVERY = _env_int("PROGRESS_EVERY", 50)


def _unknown_emails(
    session: Session,
    batch_size: int,
    *,
    job_id: int | None,
    since: datetime | None,
    strikes: int,
    bucket: int | None = None,
    bucket_of: int | None = None,
    exclude: set[str] | None = None,
) -> list[str]:
    # `retry_count < strikes` skips emails that have already burned through
    # their strikes budget — those rows still sit at verdict='unknown' but
    # we won't pay Bouncify for them again. The strike-out flip to 'invalid'
    # happens in _mark_still_unknown after the (strikes-th) attempt.
    clauses = ["verdict = 'unknown'", "retry_count < :strikes"]
    params: dict = {"limit": batch_size, "strikes": strikes}
    if job_id is not None:
        clauses.append("job_id = :jid")
        params["jid"] = job_id
    if since is not None:
        clauses.append("created_at >= :since")
        params["since"] = since
    # Hash-bucket partition for the fan-out path — each parallel workflow
    # processes one bucket. Postgres's HASHTEXT is stable + well-distributed;
    # same email → same bucket every dispatch, so zero double-processing
    # across parallel runs. SQLite (test path) doesn't have HASHTEXT, so
    # the bucket filter is skipped there.
    if bucket is not None and bucket_of and bucket_of > 1 and _is_postgres():
        clauses.append("MOD(ABS(HASHTEXT(LOWER(email))), :bucket_of) = :bucket")
        params["bucket"] = bucket
        params["bucket_of"] = bucket_of
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


def _is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def _update_rows(
    session: Session,
    email: str,
    verdict: str,
    provider_data_json: str,
    *,
    job_id: int | None,
) -> int:
    """Mark all 'unknown' rows for this email as the new (resolved) verdict.
    retry_count is reset to 0 — the email left the unknown pool so the
    strike history is moot."""
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
        f"UPDATE emailresult SET verdict = :verdict_new, provider_data = :pdata, "
        f"retry_count = 0 WHERE {where}"
    )
    return session.execute(text(sql), params).rowcount or 0


def _mark_still_unknown(
    session: Session,
    email: str,
    *,
    strikes: int,
    job_id: int | None,
) -> tuple[int, int]:
    """Increment retry_count on every still-unknown row for this email.
    If the new count reaches `strikes`, also flip verdict to 'invalid'
    in the same UPDATE — persistent unknowns are dead-MX / parked
    domains in practice, treating them as invalid stops the bleeding.

    Callers must bulk-sync struck-out emails into EmailCache themselves
    (via bulk_set_cache_invalid) — this only touches EmailResult rows.

    Returns (rows_incremented, rows_struck_out).
    """
    clauses = ["email = :email", "verdict = 'unknown'"]
    params: dict = {"email": email, "strikes": strikes}
    if job_id is not None:
        clauses.append("job_id = :jid")
        params["jid"] = job_id
    where = " AND ".join(clauses)
    # Count rows about to strike out BEFORE the UPDATE — the UPDATE
    # flips their verdict, so a post-UPDATE count by verdict='unknown'
    # would miss them.
    struck = session.execute(text(
        f"SELECT COUNT(*) FROM emailresult WHERE {where} AND retry_count + 1 >= :strikes"
    ), params).scalar() or 0
    sql = (
        f"UPDATE emailresult SET "
        f"retry_count = retry_count + 1, "
        f"verdict = CASE WHEN retry_count + 1 >= :strikes THEN 'invalid' ELSE 'unknown' END "
        f"WHERE {where}"
    )
    rows = session.execute(text(sql), params).rowcount or 0
    return rows, struck


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
    strikes: int,
) -> dict[str, int]:
    stats = {
        "resolved": 0,
        "still_unknown": 0,
        "struck_out": 0,
        "rows_updated": 0,
        "errors": 0,
    }
    done = 0
    started = time.monotonic()
    next_progress_at = PROGRESS_EVERY
    struck_out_emails: list[str] = []
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
                    rows, struck = _mark_still_unknown(
                        session, em, strikes=strikes, job_id=job_id,
                    )
                    stats["still_unknown"] += 1
                    stats["struck_out"] += struck
                    if struck:
                        struck_out_emails.append(em)
                    continue
                rows = _update_rows(
                    session, em, verdict, json.dumps(pdata), job_id=job_id,
                )
                stats["resolved"] += 1
                stats["rows_updated"] += rows
            session.commit()
        done += len(slice_)
        if done >= next_progress_at or done == len(emails):
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed > 0 else 0
            print(
                f"    {done}/{len(emails)} | resolved={stats['resolved']} "
                f"still_unknown={stats['still_unknown']} struck_out={stats['struck_out']} "
                f"errors={stats['errors']} | {rate:.1f} emails/s",
                flush=True,
            )
            next_progress_at = done + PROGRESS_EVERY
    if struck_out_emails:
        # Without this, strike-outs flip EmailResult to 'invalid' but never
        # touch EmailCache — the cache-browser undercounts invalid forever
        # even though the all-time verdict distribution shows it correctly.
        synced = bulk_set_cache_invalid(struck_out_emails, strategy="retry_unknowns_strikeout")
        print(f"  cache-synced {synced} struck-out emails as invalid", flush=True)
    return stats


async def run(args: argparse.Namespace) -> int:
    # Skip migrations — see comment in scripts/process_job.py:run.
    # Concurrent worker startups race on ALTER TABLE locks and deadlock.
    create_db_tables(skip_migrations=True)
    registry._client = httpx.AsyncClient(timeout=settings.httpx_timeout)

    providers = [p.strip() for p in (args.providers or "bouncify").split(",") if p.strip()]
    strategy = args.strategy or "bouncify_only"
    since = (
        datetime.utcnow() - timedelta(days=args.since_days)
        if args.since_days and args.since_days > 0
        else None
    )

    total = {
        "resolved": 0, "still_unknown": 0, "struck_out": 0,
        "rows_updated": 0, "errors": 0, "batches": 0,
    }
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
                    strikes=args.strikes,
                    bucket=args.bucket,
                    bucket_of=args.bucket_of,
                    exclude=attempted,
                )
            if not emails:
                print("No unknown emails left to retry.", flush=True)
                break
            attempted.update(emails)
            bucket_tag = (
                f" | bucket={args.bucket}/{args.bucket_of}"
                if args.bucket is not None and args.bucket_of else ""
            )
            print(
                f"[batch {batch_no}] {len(emails)} unknowns | "
                f"providers={providers} | strategy={strategy} | "
                f"strikes={args.strikes}{bucket_tag} | "
                f"attempted-so-far={len(attempted)}",
                flush=True,
            )
            stats = await _process_batch(
                emails, providers, strategy,
                job_id=args.job_id, strikes=args.strikes,
            )
            total["batches"] += 1
            for k in ("resolved", "still_unknown", "struck_out", "rows_updated", "errors"):
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
                f"struck_out={stats['struck_out']} "
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
    p.add_argument("--strikes", type=int, default=_env_int("UNKNOWN_STRIKES", 2),
                   help="After this many failed retries an email's verdict flips "
                        "from 'unknown' to 'invalid' so it leaves the retry pool "
                        "(default: 2, env: UNKNOWN_STRIKES).")
    p.add_argument("--bucket", type=int, default=None,
                   help="Hash-bucket index this run processes (0-based). Combined "
                        "with --bucket-of to fan a single retry sweep across N "
                        "parallel workflow runs.")
    p.add_argument("--bucket-of", type=int, default=None,
                   help="Total number of hash buckets. Postgres only — SQLite skips "
                        "the filter and processes all unknowns.")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(_parse_args())))
