"""
Standalone bulk job processor — executed by GitHub Actions.

Usage:
    python scripts/process_job.py --job-id <id>

Required env vars:
    DATABASE_URL       — PostgreSQL connection string (Neon)
    BOUNCIFY_API_KEY   — at least one provider key

Optional env vars:
    ZEROBOUNCE_API_KEY, NEVERBOUNCE_API_KEY, HUNTER_API_KEY
    CACHE_TTL_DAYS     — defaults to 30

Performance path:
    - "bouncify_only" / "local_first" with providers ⊆ {local, bouncify} →
      bulk path: local pre-filter + Bouncify's bulk API in 500-email
      sub-batches. ~10× faster than per-email for 1k+ jobs. Falls back to
      the per-email path on any verify_bulk() exception.
    - All other strategies → per-email path (consensus/waterfall need
      per-row vote logic).
"""
import argparse
import asyncio
import csv
import io
import json
import os
import sys
from pathlib import Path

# Project root must be on sys.path so `app` package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.cache import (  # noqa: E402
    get_cached,
    get_cached_many,
    parse_cached_providers,
    set_cache,
)
from app.core.validator import validate  # noqa: E402
from app.db import create_db_tables, engine  # noqa: E402
from app.models import EmailResult, Job  # noqa: E402
from app.providers import registry  # noqa: E402
from app.providers.registry import get_all_providers  # noqa: E402
from app.schemas import ProviderResult  # noqa: E402

CHUNK_SIZE = 20            # per-email path: in-flight concurrency per gather
BULK_SUB_BATCH = 500       # bulk path: emails per Bouncify bulk submission
_BULK_PROVIDERS = {"bouncify"}  # providers that have a working verify_bulk()

# Bulk path rewritten in v0.10.3 to match Bouncify's actual 5-step bulk API
# (upload+auto_verify, poll status, POST /download → CSV). The previous
# implementation submitted to wrong endpoints with the wrong body and
# produced ~82% unknown on job 10. The new implementation has a respx
# round-trip test and a >50%-unknown defensive re-verify in the worker.
#
# Still default-off until you've run at least one ~1k job with
# BOUNCIFY_BULK=1 set and confirmed verdicts match the per-email path
# on the same input. Set the env var in the GitHub Actions workflow
# secrets, or as a repo variable, then re-run.
_BULK_ENABLED = os.getenv("BOUNCIFY_BULK", "").strip().lower() in ("1", "true", "yes", "on")
# Re-verify a sub-batch per-email when more than this fraction of the
# bulk response came back "unknown" — a sanity net that catches any
# parser drift or Bouncify response-format changes before they ship
# corrupt verdicts.
_BULK_UNKNOWN_REVERIFY_PCT = 0.5


def _can_use_bulk(strategy: str, providers: list[str]) -> bool:
    """Gate the bulk path. Default off; flip BOUNCIFY_BULK=1 in env to enable."""
    if not _BULK_ENABLED:
        return False
    if strategy not in ("bouncify_only", "local_first"):
        return False
    paid = [p for p in providers if p != "local"]
    return len(paid) == 1 and paid[0] in _BULK_PROVIDERS


async def _validate_with_cache(
    email: str,
    providers: list[str],
    strategy: str,
    ttl_days: int | None = None,
) -> tuple[str, dict, bool]:
    cached = get_cached(email)
    if cached:
        return cached.verdict, parse_cached_providers(cached), True
    verdict, provider_results = await validate(email, providers, strategy)
    if verdict != "unknown" and ttl_days != 0:
        set_cache(email, verdict, provider_results, strategy, ttl_days=ttl_days)
    return verdict, provider_results, False


async def _process_sub_batch_bulk(
    emails: list[str],
    providers: list[str],
    strategy: str,
    ttl_days: int | None,
) -> list[tuple[str, dict[str, ProviderResult], bool]]:
    """Validate one sub-batch via the bulk Bouncify API.

    Falls back to per-email `bouncify.verify()` if `verify_bulk()` raises.
    Returns one (verdict, providers, from_cache) tuple per input email,
    preserving order.
    """
    n = len(emails)
    results: list[tuple[str, dict[str, ProviderResult], bool] | None] = [None] * n

    # 1. Batched cache lookup — one IN-query instead of N round-trips
    cached_map = get_cached_many(emails)
    pending_idx: list[int] = []
    pending_emails: list[str] = []
    for i, email in enumerate(emails):
        key = email.strip().lower()
        row = cached_map.get(key)
        if row:
            results[i] = (row.verdict, parse_cached_providers(row), True)
        else:
            pending_idx.append(i)
            pending_emails.append(email)

    all_providers = get_all_providers()

    # 2. Local pre-filter (only for bouncify_only — same logic as validator.py).
    #    Local runs in-process, so even sequential it's fast; gather just in case.
    if strategy == "bouncify_only" and pending_emails:
        local = all_providers.get("local")
        if local:
            local_results = await asyncio.gather(
                *[local.verify(em) for em in pending_emails],
                return_exceptions=False,
            )
            next_idx: list[int] = []
            next_emails: list[str] = []
            for rel, (abs_i, email, lr) in enumerate(
                zip(pending_idx, pending_emails, local_results)
            ):
                if lr.status == "invalid":
                    results[abs_i] = ("invalid", {"local": lr}, False)
                    # Cache the hard-invalid so the next run skips local too.
                    if ttl_days != 0:
                        try:
                            set_cache(email, "invalid", {"local": lr}, strategy, ttl_days=ttl_days)
                        except Exception as e:  # noqa: BLE001
                            print(f"[WARN] set_cache(local-invalid) failed: {e!r}", flush=True)
                else:
                    next_idx.append(abs_i)
                    next_emails.append(email)
            pending_idx = next_idx
            pending_emails = next_emails

    # 3. Bouncify bulk for everything still pending.
    if pending_emails:
        bouncify = all_providers.get("bouncify")
        if bouncify is None:
            # Shouldn't happen — _can_use_bulk gates on bouncify presence — but
            # if registry init drifted, fall through to per-email validate().
            print("[WARN] bouncify provider missing — falling back to validate()", flush=True)
            fallback = await asyncio.gather(
                *[_validate_with_cache(em, providers, strategy, ttl_days) for em in pending_emails]
            )
            for abs_i, r in zip(pending_idx, fallback):
                results[abs_i] = r
        else:
            bulk_results: list[ProviderResult]
            try:
                bulk_results = await bouncify.verify_bulk(pending_emails)
                if len(bulk_results) != len(pending_emails):
                    raise RuntimeError(
                        f"verify_bulk returned {len(bulk_results)} results for "
                        f"{len(pending_emails)} emails — shape mismatch"
                    )
            except Exception as e:  # noqa: BLE001
                print(
                    f"[WARN] bouncify.verify_bulk failed, falling back per-email: {e!r}",
                    flush=True,
                )
                bulk_results = list(await asyncio.gather(
                    *[bouncify.verify(em) for em in pending_emails],
                    return_exceptions=False,
                ))

            # Defensive net: if a large fraction of the bulk response is
            # "unknown", treat the call as failed (parser drift, Bouncify
            # response-format change, transient error) and re-verify the
            # whole sub-batch per-email. Catches the v0.10.1 regression class.
            unknown_count = sum(1 for r in bulk_results if r.status == "unknown")
            if unknown_count / max(1, len(bulk_results)) > _BULK_UNKNOWN_REVERIFY_PCT:
                print(
                    f"[WARN] bouncify bulk returned {unknown_count}/{len(bulk_results)} unknown "
                    f"(> {_BULK_UNKNOWN_REVERIFY_PCT:.0%}) — full per-email re-verify",
                    flush=True,
                )
                bulk_results = list(await asyncio.gather(
                    *[bouncify.verify(em) for em in pending_emails],
                    return_exceptions=False,
                ))
            elif unknown_count > 0:
                # Smaller miss rate — re-verify only the unknowns. Fixes the
                # long tail without paying for the whole batch again.
                unknown_idx = [i for i, r in enumerate(bulk_results) if r.status == "unknown"]
                retry = await asyncio.gather(
                    *[bouncify.verify(pending_emails[i]) for i in unknown_idx],
                    return_exceptions=False,
                )
                for j, rr in zip(unknown_idx, retry):
                    bulk_results[j] = rr

            for abs_i, email, br in zip(pending_idx, pending_emails, bulk_results):
                results[abs_i] = (br.status, {"bouncify": br}, False)
                if br.status != "unknown" and ttl_days != 0:
                    try:
                        set_cache(email, br.status, {"bouncify": br}, strategy, ttl_days=ttl_days)
                    except Exception as e:  # noqa: BLE001
                        print(f"[WARN] set_cache(bouncify) failed: {e!r}", flush=True)

    # Any leftover Nones means a code path didn't fire — defensive fallback.
    for i, r in enumerate(results):
        if r is None:
            results[i] = ("unknown", {}, False)

    return results  # type: ignore[return-value]


def _mark_failed(job_id: int, error: str) -> None:
    """Best-effort: mark a job 'failed' with a short error message."""
    try:
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if job:
                job.status = "failed"
                job.error = error[:500]
                session.add(job)
                session.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] could not mark job {job_id} failed: {e!r}", flush=True)


def _write_results(
    job_id: int,
    chunk_emails: list[str],
    chunk_results: list[tuple[str, dict[str, ProviderResult], bool]],
    new_processed: int,
) -> None:
    with Session(engine) as session:
        for email, (verdict, provider_results, _from_cache) in zip(chunk_emails, chunk_results):
            provider_data = {
                name: (res.model_dump() if hasattr(res, "model_dump") else res)
                for name, res in provider_results.items()
            }
            session.add(EmailResult(
                job_id=job_id,
                email=email,
                verdict=verdict,
                provider_data=json.dumps(provider_data),
            ))
        job = session.get(Job, job_id)
        if job:
            job.processed = new_processed
            session.add(job)
        session.commit()


async def run(job_id: int) -> None:
    create_db_tables()

    # The provider registry expects a live httpx.AsyncClient on
    # registry._client. The FastAPI lifespan hook normally sets this; the
    # standalone worker has to do it explicitly.
    registry._client = httpx.AsyncClient(timeout=settings.httpx_timeout)

    try:
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if not job:
                print(f"[ERROR] Job {job_id} not found in DB", flush=True)
                sys.exit(1)
            if not job.csv_data:
                msg = f"Job {job_id} has no csv_data — upload likely never wrote the row."
                print(f"[ERROR] {msg}", flush=True)
                _mark_failed(job_id, msg)
                sys.exit(1)
            csv_content = job.csv_data
            providers = [p.strip() for p in job.providers.split(",") if p.strip()] or ["bouncify"]
            strategy = job.strategy or "bouncify_only"

        reader = csv.DictReader(io.StringIO(csv_content))
        rows = list(reader)
        if not rows:
            msg = "CSV is empty (no data rows after header)."
            print(f"[ERROR] {msg}", flush=True)
            _mark_failed(job_id, msg)
            sys.exit(1)

        headers = list(rows[0].keys())
        email_col = next((h for h in headers if h.lower() == "email"), headers[0])
        emails = [row.get(email_col, "").strip() for row in rows]

        with Session(engine) as session:
            job = session.get(Job, job_id)
            if job:
                job.total = len(emails)
                job.status = "running"
                session.add(job)
                session.commit()

        use_bulk = _can_use_bulk(strategy, providers)
        mode = "BULK" if use_bulk else "single"
        print(
            f"Job {job_id} | {len(emails)} emails | strategy={strategy} | mode={mode}",
            flush=True,
        )
        print(f"  providers={providers}", flush=True)

        if use_bulk:
            step = BULK_SUB_BATCH
            for i in range(0, len(emails), step):
                chunk = emails[i : i + step]
                chunk_results = await _process_sub_batch_bulk(
                    chunk, providers, strategy, ttl_days=None,
                )
                done = min(i + step, len(emails))
                _write_results(job_id, chunk, chunk_results, done)
                pct = int(done / len(emails) * 100)
                print(f"  {done}/{len(emails)} ({pct}%) [bulk]", flush=True)
        else:
            step = CHUNK_SIZE
            for i in range(0, len(emails), step):
                chunk = emails[i : i + step]
                tasks = [_validate_with_cache(em, providers, strategy) for em in chunk]
                chunk_results = await asyncio.gather(*tasks)
                done = min(i + step, len(emails))
                _write_results(job_id, chunk, chunk_results, done)
                pct = int(done / len(emails) * 100)
                print(f"  {done}/{len(emails)} ({pct}%)", flush=True)

        with Session(engine) as session:
            job = session.get(Job, job_id)
            if job:
                job.status = "done"
                job.processed = len(emails)
                session.add(job)
                session.commit()

        print(f"Job {job_id} complete.", flush=True)
    finally:
        if registry._client and not registry._client.is_closed:
            await registry._client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a bulk email validation job")
    parser.add_argument("--job-id", type=int, required=True, help="Job ID to process")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.job_id))
    except SystemExit:
        raise
    except BaseException as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[FATAL] job {args.job_id} crashed: {e!r}\n{tb}", flush=True)
        _mark_failed(args.job_id, f"{type(e).__name__}: {e}")
        sys.exit(1)
