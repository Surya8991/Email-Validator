import csv
import io
import os

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
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
    # Vercel has a read-only filesystem except /tmp
    if os.getenv("VERCEL"):
        return "/tmp/uploads"
    return "uploads"


@router.post("/api/bulk", response_model=BulkJobResponse)
async def create_bulk_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    email_column: str = Form(default=""),
    providers: str = Form(default="bouncify"),
    strategy: str = Form(default="bouncify_only"),
    cache_ttl_days: int = Form(default=0),
):
    if settings.max_bulk_emails > 0:
        contents = await file.read()
        row_count = contents.count(b"\n")
        if row_count > settings.max_bulk_emails:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"CSV exceeds {settings.max_bulk_emails} email limit. "
                    "Reduce the file size or raise MAX_BULK_EMAILS."
                ),
            )
    else:
        contents = await file.read()

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
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    # 0 = no cache, >0 = custom TTL, default form value (30) uses global default
    ttl: int | None = cache_ttl_days if cache_ttl_days > 0 else (0 if cache_ttl_days == 0 else None)
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
    upload_dir = _upload_dir()
    output_path = os.path.join(upload_dir, f"results_{job_id}.csv")
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Results not ready")

    if verdict == "all":
        return FileResponse(output_path, media_type="text/csv", filename=f"validated_{job_id}.csv")

    with open(output_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if r.get("verdict", "").lower() == verdict.lower()]

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="validated_{job_id}_{verdict}.csv"'},
    )
