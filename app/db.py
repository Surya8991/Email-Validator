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


def create_db_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _apply_lightweight_migrations()


# (table, column, DDL fragment) — applied at startup with ADD COLUMN IF NOT EXISTS.
# Postgres-only; SQLite already creates everything fresh via create_all on /tmp.
_PG_COLUMN_ADDS: list[tuple[str, str, str]] = [
    ('"user"', "validation_limit", "INTEGER"),
    ("teammembership", "role", "VARCHAR DEFAULT 'member' NOT NULL"),
    ('"user"', "failed_login_count", "INTEGER DEFAULT 0 NOT NULL"),
    ('"user"', "locked_until", "TIMESTAMP"),
]


_PG_INDEX_ADDS: list[tuple[str, str]] = [
    # Functional index that makes `WHERE LOWER(email) IN (...)` index-scan
    # instead of full-scanning emailcache. The cache-lookup endpoint uses
    # LOWER() to match legacy mixed-case rows; without this index the
    # query times out on Vercel Hobby (10s) at 5k keys per call.
    ("ix_emailcache_email_lower", "emailcache (LOWER(email))"),
]


def _apply_lightweight_migrations() -> None:
    if not is_postgres():
        return
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, column, ddl in _PG_COLUMN_ADDS:
            conn.execute(text(
                f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}'
            ))
        for name, target in _PG_INDEX_ADDS:
            conn.execute(text(
                f'CREATE INDEX IF NOT EXISTS {name} ON {target}'
            ))


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
