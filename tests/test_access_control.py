"""Access-control regression tests.

Covers:
- IDOR on /jobs/{job_id} — non-admin cannot see another user's job
- IDOR on /jobs list — non-admin only sees their own jobs
- Privilege: plain admin cannot activate/deactivate a superadmin account
"""
import bcrypt
from sqlmodel import Session, select

from app.models import Job, User


def _make_user(db, email: str, role: str = "user") -> User:
    pw = bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=4)).decode()
    existing = db.exec(select(User).where(User.email == email)).first()
    if existing:
        return existing
    u = User(email=email, password_hash=pw, role=role, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_job(db, user_id: int) -> int:
    j = Job(
        user_id=user_id, strategy="bouncify_only", providers="bouncify",
        filename="x.csv", csv_data="email\n", status="done",
        total=1, processed=1,
    )
    db.add(j)
    db.commit()
    db.refresh(j)
    return j.id


# ── IDOR: /jobs/{job_id} ──────────────────────────────────────────────────────

def test_user_cannot_view_another_users_job(user_client, patch_db):
    """A regular user must get 404 when accessing a job that belongs to someone else."""
    with Session(patch_db) as db:
        other = _make_user(db, "other-owner@example.com")
        job_id = _make_job(db, other.id)

    resp = user_client.get(f"/jobs/{job_id}")
    assert resp.status_code == 404


def test_user_can_view_own_job(user_client, patch_db):
    """A regular user can view their own job."""
    with Session(patch_db) as db:
        me = db.exec(select(User).where(User.email == "regularuser@example.com")).first()
        assert me is not None
        job_id = _make_job(db, me.id)

    resp = user_client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200


# ── IDOR: /jobs list ─────────────────────────────────────────────────────────

def test_user_jobs_list_excludes_other_users_jobs(user_client, patch_db):
    """The /jobs list for a regular user must not include jobs owned by others."""
    with Session(patch_db) as db:
        other = _make_user(db, "jobs-list-other@example.com")
        other_job_id = _make_job(db, other.id)

    resp = user_client.get("/jobs")
    assert resp.status_code == 200
    # The other user's job id must not appear in the page
    assert str(other_job_id) not in resp.text or \
        f"/jobs/{other_job_id}" not in resp.text


# ── Privilege: activate/deactivate a superadmin requires superadmin ────────────

def test_admin_cannot_deactivate_superadmin(auth_client, patch_db):
    """A plain admin (auth_client fixture is role=admin) must get 403 when
    trying to deactivate a superadmin account."""
    with Session(patch_db) as db:
        sa = _make_user(db, "protected-superadmin@example.com", role="superadmin")
        sa_id = sa.id

    resp = auth_client.post(f"/admin/users/{sa_id}/deactivate")
    assert resp.status_code == 403


def test_admin_cannot_activate_superadmin(auth_client, patch_db):
    """A plain admin must get 403 when trying to activate a superadmin account."""
    with Session(patch_db) as db:
        sa = _make_user(db, "inactive-superadmin@example.com", role="superadmin")
        sa.is_active = False
        db.add(sa)
        db.commit()
        sa_id = sa.id

    resp = auth_client.post(f"/admin/users/{sa_id}/activate")
    assert resp.status_code == 403
