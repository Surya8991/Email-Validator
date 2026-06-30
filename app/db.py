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
    # Strike-count for the 2-strikes rule in scripts/retry_unknowns.py.
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

    Strategy is "check, then act with a deadline":

    1. Take a Postgres session-scoped advisory lock on a SEPARATE
       connection. Other concurrent migration callers see the lock held
       and return immediately. Putting the lock on its own connection
       means a transaction abort in the DDL block can't poison the
       unlock with `InFailedSqlTransaction`.
    2. For each ADD COLUMN / CREATE INDEX, query `information_schema`
       (and `pg_indexes`) first. If the column / index already exists,
       skip — no ACCESS EXCLUSIVE attempt at all. After the first
       successful migration run, every subsequent call is purely
       read-only against `information_schema` and zero deadlock risk.
    3. The (rare) actual DDL goes in its own short transaction with
       `SET LOCAL lock_timeout='5s'` so a held ACCESS SHARE from a
       sibling worker fails the ALTER fast with a clear error instead
       of deadlock-aborting the whole connection.

    Original deadlock that killed job 101 (and later db_init.yml runs
    on PR #29 / #30) was the previous version's `engine.begin()` block
    holding the connection through a series of ACCESS EXCLUSIVE ALTERs
    while sibling bulk workers held ACCESS SHARE. Postgres's deadlock
    detector picked us as the victim."""
    if not is_postgres():
        return
    from sqlalchemy import text

    # Acquire the advisory lock on its own connection so a poisoned
    # DDL transaction further down can't bork the unlock.
    lock_conn = engine.connect()
    try:
        got_lock = lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _MIGRATION_ADVISORY_LOCK_KEY},
        ).scalar()
        lock_conn.commit()
        if not got_lock:
            return  # another caller is running migrations
        try:
            _run_pending_migrations()
        finally:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": _MIGRATION_ADVISORY_LOCK_KEY},
            )
            lock_conn.commit()
    finally:
        lock_conn.close()


def _run_pending_migrations() -> None:
    from sqlalchemy import text
    with engine.connect() as conn:
        for table, column, ddl in _PG_COLUMN_ADDS:
            # Strip quotes from quoted-identifier table names like '"user"'.
            clean_table = table.strip('"')
            exists = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": clean_table, "c": column},
            ).first()
            if exists:
                continue  # nothing to do — column already there
            try:
                with conn.begin():
                    # Fail fast if another worker holds ACCESS SHARE,
                    # rather than waiting for the deadlock detector.
                    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                    conn.execute(text(
                        f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'
                    ))
            except Exception as e:  # noqa: BLE001
                # Don't kill the whole migration run for one stubborn
                # table — the next caller will retry it.
                print(
                    f"[migration] skip ALTER {table}.{column}: "
                    f"{type(e).__name__}: {str(e)[:160]}",
                    flush=True,
                )

        for name, target in _PG_INDEX_ADDS:
            idx_exists = conn.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
                {"n": name},
            ).first()
            if idx_exists:
                continue
            try:
                with conn.begin():
                    conn.execute(text("SET LOCAL lock_timeout = '10s'"))
                    conn.execute(text(f'CREATE INDEX {name} ON {target}'))
            except Exception as e:  # noqa: BLE001
                print(
                    f"[migration] skip CREATE INDEX {name}: "
                    f"{type(e).__name__}: {str(e)[:160]}",
                    flush=True,
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
