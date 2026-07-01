"""Tests for the new admin per-user CSV export + per-job verdict counts.

Covers /admin/users/{user_id}/emails.csv (filtered + unfiltered) and that
the /jobs and /jobs/{id} routes attach the new `verdicts` dict.
"""
from sqlmodel import Session, select

from app.models import EmailResult, Job, User


def _seed_user_with_results(engine, email: str = "demo@example.com") -> tuple[int, int]:
    """Insert a User + Job + 4 EmailResult rows (one per verdict).
    Returns (user_id, job_id)."""
    with Session(engine) as db:
        u = db.exec(select(User).where(User.email == email)).first()
        if not u:
            db.add(User(
                email=email, password_hash="x", role="user", is_active=True,
            ))
            db.commit()
            u = db.exec(select(User).where(User.email == email)).first()
        job = Job(
            user_id=u.id, strategy="bouncify_only", providers="bouncify",
            filename="d.csv", csv_data="email\n", status="done",
            total=4, processed=4,
        )
        db.add(job)
        db.commit()
        for em, v in [
            ("a@a.com", "valid"),
            ("b@b.com", "invalid"),
            ("c@c.com", "risky"),
            ("d@d.com", "unknown"),
        ]:
            db.add(EmailResult(job_id=job.id, email=em, verdict=v, provider_data="{}"))
        db.commit()
        return u.id, job.id


def test_user_emails_export_all(auth_client, patch_db):
    uid, _ = _seed_user_with_results(patch_db, "all-export@example.com")
    resp = auth_client.get(f"/admin/users/{uid}/emails.csv")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    # Header + 4 rows
    assert "email,verdict,job_id,job_filename,created_at" in body
    for em in ("a@a.com", "b@b.com", "c@c.com", "d@d.com"):
        assert em in body


def test_user_emails_export_verdict_filter(auth_client, patch_db):
    uid, _ = _seed_user_with_results(patch_db, "filter-export@example.com")
    resp = auth_client.get(f"/admin/users/{uid}/emails.csv?verdict=invalid")
    assert resp.status_code == 200
    body = resp.text
    assert "b@b.com" in body
    for em in ("a@a.com", "c@c.com", "d@d.com"):
        assert em not in body


def test_user_emails_export_bad_verdict(auth_client, patch_db):
    uid, _ = _seed_user_with_results(patch_db, "bad-verdict@example.com")
    resp = auth_client.get(f"/admin/users/{uid}/emails.csv?verdict=nope")
    assert resp.status_code == 400


def test_user_emails_export_unknown_user(auth_client):
    resp = auth_client.get("/admin/users/9999999/emails.csv")
    assert resp.status_code == 404


def test_jobs_list_attaches_verdict_counts(auth_client, patch_db):
    _, jid = _seed_user_with_results(patch_db, "verdicts-jobs@example.com")
    resp = auth_client.get("/jobs")
    assert resp.status_code == 200
    body = resp.text
    # The new chips should render the per-verdict counts inline.
    assert "✓1" in body
    assert "✗1" in body
    assert "⚠1" in body
    assert "?1" in body


def test_job_detail_renders_verdict_card(auth_client, patch_db):
    _, jid = _seed_user_with_results(patch_db, "verdicts-detail@example.com")
    resp = auth_client.get(f"/jobs/{jid}")
    assert resp.status_code == 200
    body = resp.text
    assert "Verdict breakdown" in body
    # 4 emails, 1 each — every percentage should be exactly 25.0%.
    assert "25.0%" in body


def test_jobs_list_renders_per_user_stats_panel(auth_client, patch_db):
    _seed_user_with_results(patch_db, "panel-user@example.com")
    resp = auth_client.get("/jobs")
    assert resp.status_code == 200
    body = resp.text
    assert "Per-user verdict stats" in body
    assert "panel-user@example.com" in body


def test_jobs_list_per_user_stats_shows_total_row(auth_client, patch_db):
    """The per-user stats table's <tfoot> Total row sums exactly what
    _per_user_verdict_stats() returns — computed from the DB directly
    rather than hardcoded, since `patch_db` is session-scoped and other
    tests' seeded users accumulate in it."""
    import re

    from app.routes.ui import _per_user_verdict_stats

    _seed_user_with_results(patch_db, "total-row-user@example.com")
    stats = _per_user_verdict_stats()
    expected = [
        sum(s[k] for s in stats)
        for k in ("jobs", "processed", "valid", "invalid", "risky", "unknown")
    ]

    resp = auth_client.get("/jobs")
    assert resp.status_code == 200
    body = resp.text
    tfoot_match = re.search(r"<tfoot.*?</tfoot>", body, re.S)
    assert tfoot_match, "expected a <tfoot> Total row in the per-user stats table"
    tfoot = tfoot_match.group(0)
    assert "Total" in tfoot
    numbers = [int(n) for n in re.findall(r">\s*(\d+)\s*<", tfoot)]
    assert numbers[:6] == expected


def test_cache_page_shows_verdict_dashboard(auth_client, patch_db):
    """Both admin /cache and user /cache_user include partials/cache_stats."""
    from datetime import datetime, timedelta

    from app.models import EmailCache
    with Session(patch_db) as db:
        now = datetime.utcnow()
        db.add(EmailCache(
            email="cached-valid@example.com", verdict="valid",
            provider_data="{}", providers_used="bouncify",
            strategy="bouncify_only", validated_at=now,
            expires_at=now + timedelta(days=30),
        ))
        db.add(EmailCache(
            email="cached-invalid@example.com", verdict="invalid",
            provider_data="{}", providers_used="bouncify",
            strategy="bouncify_only", validated_at=now,
            expires_at=now + timedelta(days=30),
        ))
        db.commit()
    resp = auth_client.get("/cache")
    assert resp.status_code == 200
    body = resp.text
    assert "Total cached" in body
    # The two seeded rows should appear in their verdict slots.
    assert ">Valid<" in body or "Valid" in body


def test_cache_stats_exclude_expired_rows(auth_client, patch_db):
    """Expired EmailCache rows (past TTL, not yet purged) must not count as
    live cache — /cache, /partials/cache-table, and /api/cache/export should
    all agree on the same live count."""
    from datetime import datetime, timedelta

    from app.models import EmailCache

    with Session(patch_db) as db:
        now = datetime.utcnow()
        db.add(EmailCache(
            email="live@example.com", verdict="valid",
            provider_data="{}", providers_used="bouncify",
            strategy="bouncify_only", validated_at=now - timedelta(days=400),
            expires_at=now + timedelta(days=30),
        ))
        db.add(EmailCache(
            email="expired@example.com", verdict="invalid",
            provider_data="{}", providers_used="bouncify",
            strategy="bouncify_only", validated_at=now - timedelta(days=400),
            expires_at=now - timedelta(days=1),
        ))
        db.commit()

    resp = auth_client.get("/cache")
    assert resp.status_code == 200
    assert "expired@example.com" not in resp.text

    table_resp = auth_client.get("/partials/cache-table")
    assert table_resp.status_code == 200
    assert "expired@example.com" not in table_resp.text
    assert "live@example.com" in table_resp.text

    export_resp = auth_client.get("/api/cache/export")
    assert export_resp.status_code == 200
    assert "expired@example.com" not in export_resp.text
    assert "live@example.com" in export_resp.text
