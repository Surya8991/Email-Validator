import csv
import io
import json
import os

import httpx
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import EmailResult, Job
from app.schemas import BulkJobResponse, BulkStatusResponse
from app.workers.bulk_worker import process_bulk_job

router = APIRouter()


def _upload_dir() -> str:
    if settings.upload_dir:
        return settings.upload_dir
    if os.getenv("VERCEL"):
        return "/tmp/uploads"
    return "uploads"


async def _trigger_github_actions(job_id: int) -> bool:
    """Trigger bulk_process.yml workflow_dispatch for this job. Returns True on success."""
    if not settings.github_pat or not settings.github_repo:
        return False
    try:
        owner, repo = settings.github_repo.split("/", 1)
    except ValueError:
        return False
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/bulk_process.yml/dispatches"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.github_pat}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"ref": "main", "inputs": {"job_id": str(job_id)}},
            )
        return resp.status_code == 204
    except Exception:
        return False


@router.post("/api/bulk", response_model=BulkJobResponse)
async def create_bulk_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    email_column: str = Form(default=""),
    providers: str = Form(default="bouncify"),
    strategy: str = Form(default="bouncify_only"),
    cache_ttl_days: int = Form(default=0),
):
    contents = await file.read()

    if settings.max_bulk_emails > 0:
        row_count = contents.count(b"\n")
        if row_count > settings.max_bulk_emails:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"CSV exceeds {settings.max_bulk_emails} email limit. "
                    "Reduce the file size or raise MAX_BULK_EMAILS."
                ),
            )

    # Decode for DB storage (GitHub Actions reads csv_data from DB)
    csv_str = contents.decode("utf-8-sig")  # strips BOM if present

    # Also write to disk for local BackgroundTask fallback
    upload_dir = _upload_dir()
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, f"upload_{id(contents)}.csv")
    with open(filepath, "wb") as f:
        f.write(contents)

    with Session(engine) as session:
        job = Job(
            strategy=strategy,
            providers=providers,
            filename=file.filename,
            csv_data=csv_str,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    ttl: int | None = cache_ttl_days if cache_ttl_days > 0 else (0 if cache_ttl_days == 0 else None)

    # Try GitHub Actions first — falls back to BackgroundTasks if PAT not set
    triggered = await _trigger_github_actions(job_id)
    if not triggered:
        background_tasks.add_task(
            process_bulk_job, job_id, filepath, email_column, providers.split(","), strategy, ttl
        )

    return BulkJobResponse(job_id=job_id, total=0, status="queued")


@router.get("/api/bulk/{job_id}", response_model=BulkStatusResponse)
async def get_bulk_status(job_id: int):
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        results = session.exec(select(EmailResult).where(EmailResult.job_id == job_id)).all()

    summary: dict[str, int] = {"valid": 0, "invalid": 0, "risky": 0, "unknown": 0}
    for r in results:
        summary[r.verdict] = summary.get(r.verdict, 0) + 1

    download_url = f"/api/bulk/{job_id}/download" if job.status == "done" else None
    return BulkStatusResponse(
        job_id=job_id,
        status=job.status,
        progress=job.processed,
        total=job.total,
        summary=summary,
        download_url=download_url,
    )


@router.get("/api/bulk/{job_id}/download")
async def download_bulk(job_id: int, verdict: str = "all"):
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job or job.status != "done":
            raise HTTPException(status_code=404, detail="Results not ready")
        results = session.exec(
            select(EmailResult).where(EmailResult.job_id == job_id)
        ).all()

    if verdict != "all":
        results = [r for r in results if r.verdict.lower() == verdict.lower()]

    # Build column list from first result's provider_data
    provider_cols: list[str] = []
    if results:
        try:
            pd = json.loads(results[0].provider_data)
            provider_cols = [f"{p}_status" for p in pd]
        except Exception:
            pass

    fieldnames = ["email", "verdict"] + provider_cols + ["from_cache"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in results:
        row: dict = {"email": r.email, "verdict": r.verdict, "from_cache": False}
        try:
            pd = json.loads(r.provider_data)
            for p, data in pd.items():
                row[f"{p}_status"] = data.get("status", "")
        except Exception:
            pass
        writer.writerow(row)

    suffix = f"_{verdict}" if verdict != "all" else ""
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="validated_{job_id}{suffix}.csv"'},
    )
