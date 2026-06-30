"""Tests for the new MAX_QUEUED_WORKFLOW_RUNS gate on /api/bulk and
/admin/retry-unknowns. The cap is enforced via a best-effort call to the
GitHub Actions REST API; both endpoints return 429 when the workflow's
'queued' count is already at the cap.
"""
from unittest.mock import AsyncMock, patch

# Import the model module so SQLModel.metadata registers tables BEFORE
# conftest's `patch_db` runs create_all — otherwise an isolated run of
# this file alone hits 'no such table: user' on the auth_client login.
from app import models  # noqa: F401


def test_bulk_refused_when_queue_full(auth_client):
    """POST /api/bulk → 429 when _count_queued_workflow_runs reports >= cap.
    Disable per-user caps so the queue check is what fires (other tests in
    the session may have already pushed test@example.com over the per-user
    limit, masking the queue gate)."""
    from app.config import settings
    csv = "email\nfoo@example.com\n"
    with patch.object(settings, "max_user_active_jobs", 0), \
         patch.object(settings, "max_user_active_emails", 0), \
         patch(
             "app.routes.api_bulk._count_queued_workflow_runs",
             new=AsyncMock(return_value=99),
         ):
        resp = auth_client.post(
            "/api/bulk",
            files={"file": ("t.csv", csv, "text/csv")},
            data={"strategy": "bouncify_only", "providers": "bouncify"},
        )
    assert resp.status_code == 429
    assert "queue is full" in resp.json()["detail"].lower()


def test_bulk_passes_when_queue_under_cap(auth_client):
    """When _count_queued_workflow_runs returns 0, the queue gate doesn't
    fire — verify by asserting the response is NOT a 429-queue-full."""
    from app.config import settings
    csv = "email\nfoo@example.com\n"
    with patch.object(settings, "max_user_active_jobs", 0), \
         patch.object(settings, "max_user_active_emails", 0), \
         patch(
             "app.routes.api_bulk._count_queued_workflow_runs",
             new=AsyncMock(return_value=0),
         ), patch(
             "app.routes.api_bulk._trigger_github_actions",
             new=AsyncMock(return_value=(True, None)),
         ):
        resp = auth_client.post(
            "/api/bulk",
            files={"file": ("t.csv", csv, "text/csv")},
            data={"strategy": "bouncify_only", "providers": "bouncify"},
        )
    # Anything but a queue-cap 429 is fine — could be 200 (created) or a
    # different downstream error (missing GITHUB_PAT in tests, etc.).
    if resp.status_code == 429:
        assert "queue is full" not in resp.json().get("detail", "").lower()


def test_retry_unknowns_refused_when_queue_full(auth_client):
    """POST /admin/retry-unknowns → 429 when the retry workflow's queue
    is already at cap. Stubs github_pat/repo + the queued-count helper."""
    from app.config import settings
    with patch.object(settings, "github_pat", "fake-pat"), \
         patch.object(settings, "github_repo", "owner/repo"), \
         patch(
             "app.routes.api_bulk._count_queued_workflow_runs",
             new=AsyncMock(return_value=99),
         ):
        resp = auth_client.post("/admin/retry-unknowns?num_buckets=3")
    assert resp.status_code == 429
    assert "retry queue is full" in resp.json()["detail"].lower()
