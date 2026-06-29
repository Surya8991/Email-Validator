"""Tests for the /workflow-callback and /retry endpoints added to handle
externally-cancelled runs and manual job re-dispatch."""
from unittest.mock import AsyncMock, patch

from sqlmodel import Session

from app.models import EmailResult, Job, User


def _seed_job(engine, status: str = "failed", error: str | None = "boom") -> int:
    """Insert a Job owned by test@example.com (auth_client's user). Returns id."""
    with Session(engine) as db:
        user = db.exec(
            __import__("sqlmodel").select(User).where(User.email == "test@example.com")
        ).first()
        assert user is not None, "auth_client fixture should have created this user"
        job = Job(
            user_id=user.id,
            strategy="bouncify_only",
            providers="bouncify",
            filename="t.csv",
            csv_data="email\nx@y.com\n",
            status=status,
            total=1,
            processed=0,
            error=error,
        )
        db.add(job)
        db.commit()
        return job.id


# -------------------- /workflow-callback --------------------

def test_workflow_callback_rejects_without_token_configured(auth_client, patch_db):
    job_id = _seed_job(patch_db, status="running", error=None)
    # settings.job_callback_token is "" by default → 503
    resp = auth_client.post(
        f"/api/bulk/{job_id}/workflow-callback",
        json={"conclusion": "cancelled"},
        headers={"X-Callback-Token": "anything"},
    )
    assert resp.status_code == 503


def test_workflow_callback_rejects_bad_token(auth_client, patch_db):
    from app.config import settings
    job_id = _seed_job(patch_db, status="running", error=None)
    with patch.object(settings, "job_callback_token", "correct-token"):
        resp = auth_client.post(
            f"/api/bulk/{job_id}/workflow-callback",
            json={"conclusion": "cancelled"},
            headers={"X-Callback-Token": "wrong-token"},
        )
    assert resp.status_code == 401


def test_workflow_callback_marks_cancelled_run_failed(auth_client, patch_db):
    from app.config import settings
    job_id = _seed_job(patch_db, status="running", error=None)
    run_url = "https://github.com/owner/repo/actions/runs/12345"
    with patch.object(settings, "job_callback_token", "secret"):
        resp = auth_client.post(
            f"/api/bulk/{job_id}/workflow-callback",
            json={"conclusion": "cancelled", "run_url": run_url},
            headers={"X-Callback-Token": "secret"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"

    with Session(patch_db) as db:
        job = db.get(Job, job_id)
        assert job.status == "failed"
        assert "cancelled" in (job.error or "").lower()
        assert run_url in (job.error or "")


def test_workflow_callback_noop_on_already_done(auth_client, patch_db):
    """A late callback for a job the worker already wrapped up must not
    overwrite the successful terminal state."""
    from app.config import settings
    job_id = _seed_job(patch_db, status="done", error=None)
    with patch.object(settings, "job_callback_token", "secret"):
        resp = auth_client.post(
            f"/api/bulk/{job_id}/workflow-callback",
            json={"conclusion": "failure"},
            headers={"X-Callback-Token": "secret"},
        )
    assert resp.status_code == 200
    assert resp.json().get("noop") is True

    with Session(patch_db) as db:
        job = db.get(Job, job_id)
        assert job.status == "done"  # not clobbered


def test_workflow_callback_success_on_running_marks_failed(auth_client, patch_db):
    """Workflow says 'success' but the worker never wrote 'done' — that's a
    real failure mode and should be surfaced, not silently passed."""
    from app.config import settings
    job_id = _seed_job(patch_db, status="running", error=None)
    with patch.object(settings, "job_callback_token", "secret"):
        resp = auth_client.post(
            f"/api/bulk/{job_id}/workflow-callback",
            json={"conclusion": "success", "run_url": "https://gh/run/9"},
            headers={"X-Callback-Token": "secret"},
        )
    assert resp.status_code == 200
    with Session(patch_db) as db:
        job = db.get(Job, job_id)
        assert job.status == "failed"
        assert "never marked done" in (job.error or "")


# -------------------- /retry --------------------

def test_retry_rejects_non_failed_job(auth_client, patch_db):
    job_id = _seed_job(patch_db, status="running", error=None)
    resp = auth_client.post(f"/api/bulk/{job_id}/retry")
    assert resp.status_code == 409
    assert "failed" in resp.json()["detail"].lower()


def test_retry_dispatches_and_resets_state(auth_client, patch_db):
    job_id = _seed_job(patch_db, status="failed", error="previous run died")
    # Seed a couple of leftover result rows from the prior run.
    with Session(patch_db) as db:
        db.add(EmailResult(job_id=job_id, email="a@b.com", verdict="valid", provider_data="{}"))
        db.add(EmailResult(job_id=job_id, email="c@d.com", verdict="invalid", provider_data="{}"))
        db.commit()

    with patch(
        "app.routes.api_bulk._trigger_github_actions",
        new=AsyncMock(return_value=(True, None)),
    ) as mock_trigger:
        resp = auth_client.post(f"/api/bulk/{job_id}/retry")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "job_id": job_id, "status": "queued"}
    mock_trigger.assert_awaited_once()
    # current_user.email passed through so the GHA run-name is meaningful
    assert mock_trigger.call_args.kwargs.get("triggered_by") == "test@example.com"

    with Session(patch_db) as db:
        job = db.get(Job, job_id)
        assert job.status == "queued"
        assert job.processed == 0
        assert job.error is None
        leftover = db.exec(
            __import__("sqlmodel").select(EmailResult).where(EmailResult.job_id == job_id)
        ).all()
        assert leftover == []


def test_retry_flips_back_to_failed_when_dispatch_errors(auth_client, patch_db):
    job_id = _seed_job(patch_db, status="failed", error="x")
    with patch(
        "app.routes.api_bulk._trigger_github_actions",
        new=AsyncMock(return_value=(False, "GitHub 422 bad inputs")),
    ):
        resp = auth_client.post(f"/api/bulk/{job_id}/retry")
    assert resp.status_code == 502
    with Session(patch_db) as db:
        job = db.get(Job, job_id)
        assert job.status == "failed"
        assert "422" in (job.error or "")
