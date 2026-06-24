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
"""
import argparse
import asyncio
import csv
import io
import json
import sys
from pathlib import Path

# Project root must be on sys.path so `app` package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session  # noqa: E402

from app.core.cache import get_cached, parse_cached_providers, set_cache  # noqa: E402
from app.core.validator import validate  # noqa: E402
from app.db import create_db_tables, engine  # noqa: E402
from app.models import EmailResult, Job  # noqa: E402

CHUNK_SIZE = 20


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


async def run(job_id: int) -> None:
    create_db_tables()

    # Read job from DB
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            print(f"[ERROR] Job {job_id} not found in DB", flush=True)
            sys.exit(1)
        if not job.csv_data:
            print(f"[ERROR] Job {job_id} has no csv_data", flush=True)
            sys.exit(1)
        csv_content = job.csv_data
        providers = [p.strip() for p in job.providers.split(",") if p.strip()] or ["bouncify"]
        strategy = job.strategy or "bouncify_only"

    # Parse CSV
    reader = csv.DictReader(io.StringIO(csv_content))
    rows = list(reader)
    if not rows:
        print("[ERROR] CSV is empty", flush=True)
        sys.exit(1)

    headers = list(rows[0].keys())
    email_col = next((h for h in headers if h.lower() == "email"), headers[0])
    emails = [(i, row.get(email_col, "").strip()) for i, row in enumerate(rows)]

    # Mark job as running
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job:
            job.total = len(emails)
            job.status = "running"
            session.add(job)
            session.commit()

    print(f"Job {job_id} | {len(emails)} emails | strategy={strategy}", flush=True)
    print(f"  providers={providers}", flush=True)

    for i in range(0, len(emails), CHUNK_SIZE):
        chunk = emails[i : i + CHUNK_SIZE]
        tasks = [_validate_with_cache(email, providers, strategy) for _, email in chunk]
        results = await asyncio.gather(*tasks)

        with Session(engine) as session:
            for (_, email), (verdict, provider_results, _from_cache) in zip(chunk, results):
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
                job.processed = min(i + CHUNK_SIZE, len(emails))
                session.add(job)
            session.commit()

        done = min(i + CHUNK_SIZE, len(emails))
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a bulk email validation job")
    parser.add_argument("--job-id", type=int, required=True, help="Job ID to process")
    args = parser.parse_args()
    asyncio.run(run(args.job_id))
