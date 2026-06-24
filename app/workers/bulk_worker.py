import asyncio
import json
import os

from sqlmodel import Session

from app.core.cache import get_cached, parse_cached_providers, set_cache
from app.core.csv_io import parse_csv_emails, write_results_csv
from app.core.validator import validate
from app.db import engine
from app.models import EmailResult, Job


def _upload_dir() -> str:
    from app.config import settings
    if settings.upload_dir:
        return settings.upload_dir
    if os.getenv("VERCEL"):
        return "/tmp/uploads"
    return "uploads"


CHUNK_SIZE = 20


async def _validate_with_cache(
    email: str,
    providers: list[str],
    strategy: str,
    ttl_days: int | None = None,
) -> tuple[str, dict, bool]:
    """Returns (verdict, provider_data_dict, from_cache)."""
    cached = get_cached(email)
    if cached:
        return cached.verdict, parse_cached_providers(cached), True

    verdict, provider_results = await validate(email, providers, strategy)
    if verdict != "unknown" and ttl_days != 0:
        set_cache(email, verdict, provider_results, strategy, ttl_days=ttl_days)
    return verdict, provider_results, False


async def process_bulk_job(
    job_id: int,
    filepath: str,
    email_column: str,
    providers: list[str],
    strategy: str,
    ttl_days: int | None = None,
) -> None:
    rows = await parse_csv_emails(filepath, email_column)

    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            return
        job.total = len(rows)
        job.status = "running"
        session.add(job)
        session.commit()

    original_rows: list[dict] = []
    all_results: list[dict] = []
    email_col = email_column

    for i in range(0, len(rows), CHUNK_SIZE):
        chunk = rows[i : i + CHUNK_SIZE]
        tasks = [
            _validate_with_cache(email, providers, strategy, ttl_days) for _, email, _ in chunk
        ]
        verdicts = await asyncio.gather(*tasks)

        with Session(engine) as session:
            for (row_idx, email, orig_row), (verdict, provider_results, from_cache) in zip(
                chunk, verdicts
            ):
                if not email_col and orig_row:
                    for k, v in orig_row.items():
                        if v.strip() == email:
                            email_col = k
                            break
                original_rows.append(orig_row)
                provider_data = {
                    name: (
                        res.model_dump() if hasattr(res, "model_dump") else res
                    )
                    for name, res in provider_results.items()
                }
                all_results.append(
                    {
                        "email": email,
                        "verdict": verdict,
                        "providers": provider_results,
                        "from_cache": from_cache,
                    }
                )
                er = EmailResult(
                    job_id=job_id,
                    email=email,
                    verdict=verdict,
                    provider_data=json.dumps(provider_data),
                )
                session.add(er)
            job = session.get(Job, job_id)
            if job:
                job.processed = min(i + CHUNK_SIZE, len(rows))
                session.add(job)
            session.commit()

    output_path = os.path.join(_upload_dir(), f"results_{job_id}.csv")
    write_results_csv(original_rows, email_col or "email", all_results, output_path)

    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job:
            job.status = "done"
            job.processed = len(rows)
            session.add(job)
            session.commit()
