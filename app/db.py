import os

from sqlmodel import Session, SQLModel, create_engine


def _db_url() -> str:
    from app.config import settings
    if settings.database_url:
        url = settings.database_url
        # SQLAlchemy 2.x requires explicit dialect — normalize Neon/Supabase URLs
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql://") and "+psycopg" not in url:
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url
    # Auto-detect Vercel (read-only filesystem — must use /tmp)
    if os.getenv("VERCEL"):
        return "sqlite:////tmp/email_validator.db"
    return "sqlite:///./email_validator.db"


def _engine_kwargs() -> dict:
    url = _db_url()
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    # Neon idle-pauses connections; pre-ping + short recycle avoids stale-conn 500s.
    return {
        "connect_args": {"sslmode": "require", "connect_timeout": 5},
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }


DATABASE_URL = _db_url()
engine = create_engine(DATABASE_URL, **_engine_kwargs())


def create_db_tables(skip_migrations: bool = False) -> None:
    """Ensure all SQLModel tables exist + (optionally) run lightweight
    migrations.

    `skip_migrations=True` is used by worker scripts (process_job.py,
    retry_unknowns.py, ...) that run inside a GitHub Actions worker
    concurrently with other workers. Without it, each concurrent
    worker tries to grab `ACCESS EXCLUSIVE` on every table being
    ALTERed while siblings hold `ACCESS SHARE` from regular SELECTs,
    and Postgres deadlock-detects one of them dead (job 101 in prod).
    Migrations are run from db_init.yml on every push to main, so
    workers don't need to re-do that work."""
    SQLModel.metadata.create_all(engine)
    if not skip_migrations:
        _apply_lightweight_migrations()


# (table, column, DDL fragment) — applied at startup with ADD COLUMN IF NOT EXISTS.
# Postgres-only; SQLite already creates everything fresh via create_all on /tmp.
_PG_COLUMN_ADDS: list[tuple[str, str, str]] = [
    ('"user"', "validation_limit", "INTEGER"),
    ("teammembership", "role", "VARCHAR DEFAULT 'member' NOT NULL"),
    ('"user"', "failed_login_count", "INTEGER DEFAULT 0 NOT NULL"),
    ('"user"', "locked_until", "TIMESTAMP"),
    # Strike-count for the 3-strikes rule in scripts/retry_unknowns.py.
    ("emailresult", "retry_count", "INTEGER DEFAULT 0 NOT NULL"),
]


_PG_INDEX_ADDS: list[tuple[str, str]] = [
    # Functional index that makes `WHERE LOWER(email) IN (...)` index-scan
    # instead of full-scanning emailcache. The cache-lookup endpoint uses
    # LOWER() to match legacy mixed-case rows; without this index the
    # query times out on Vercel Hobby (10s) at 5k keys per call.
    ("ix_emailcache_email_lower", "emailcache (LOWER(email))"),
]


_MIGRATION_ADVISORY_LOCK_KEY = 7331  # arbitrary; just needs to be unique-per-purpose


def _apply_lightweight_migrations() -> None:
    """Run ALTER TABLE / CREATE INDEX statements that aren't covered by
    `SQLModel.metadata.create_all` (which only creates missing tables, not
    new columns on existing ones).

    Wrapped in a Postgres advisory lock so concurrent callers don't
    serialize on `ACCESS EXCLUSIVE` (which is what caused the deadlock
    that killed job 101 in prod when two workers ALTER'd `emailresult`
    while a third held an `ACCESS SHARE` from a SELECT). Only one caller
    actually runs the DDL; others return immediately. Idempotent — the
    `IF NOT EXISTS` clauses make repeated runs no-ops anyway."""
    if not is_postgres():
        return
    from sqlalchemy import text
    with engine.begin() as conn:
        got_lock = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _MIGRATION_ADVISORY_LOCK_KEY},
        ).scalar()
        if not got_lock:
            # Someone else is running migrations; trust them and move on.
            return
        try:
            for table, column, ddl in _PG_COLUMN_ADDS:
                conn.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}'
                ))
            for name, target in _PG_INDEX_ADDS:
                conn.execute(text(
                    f'CREATE INDEX IF NOT EXISTS {name} ON {target}'
                ))
        finally:
            conn.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": _MIGRATION_ADVISORY_LOCK_KEY},
            )


def backfill_team_owners() -> None:
    """For teams without any owner membership, create one from Team.created_by.

    Idempotent — only inserts when no owner exists for a team. Run on startup.
    """
    from sqlmodel import Session, select

    from app.models import Team, TeamMembership, User
    with Session(engine) as db:
        teams = db.exec(select(Team)).all()
        for t in teams:
            if t.created_by is None:
                continue
            existing_owner = db.exec(
                select(TeamMembership).where(
                    TeamMembership.team_id == t.id,
                    TeamMembership.role == "owner",
                )
            ).first()
            if existing_owner:
                continue
            creator = db.get(User, t.created_by)
            if creator is None:
                continue
            # If the creator already has a non-owner membership, promote it; otherwise create one.
            existing_m = db.exec(
                select(TeamMembership).where(
                    TeamMembership.team_id == t.id,
                    TeamMembership.user_id == creator.id,
                )
            ).first()
            if existing_m:
                existing_m.role = "owner"
                existing_m.status = "active"
            else:
                db.add(TeamMembership(
                    team_id=t.id,
                    user_id=creator.id,
                    status="active",
                    role="owner",
                    approved_at=t.created_at,
                    approved_by=creator.id,
                ))
        db.commit()


def get_session():
    with Session(engine) as session:
        yield session


def is_postgres() -> bool:
    return _db_url().startswith("postgresql")
