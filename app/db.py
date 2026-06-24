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


def _connect_args() -> dict:
    url = _db_url()
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    # Neon + most managed Postgres require SSL
    return {"sslmode": "require"}


DATABASE_URL = _db_url()
engine = create_engine(DATABASE_URL, connect_args=_connect_args())


def create_db_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
