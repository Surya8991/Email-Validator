from fastapi import APIRouter
from sqlalchemy import text
from sqlmodel import Session

from app.db import engine, is_postgres
from app.providers.registry import get_enabled_providers

router = APIRouter()


@router.get("/api/health")
async def health():
    # Trivial SELECT 1 so an external keep-warm ping wakes the DB too,
    # not just the Vercel function. Failures are reported, not raised.
    db_ok = False
    try:
        with Session(engine) as db:
            db.exec(text("SELECT 1")).first()
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "providers_enabled": get_enabled_providers(),
        "database": "postgresql" if is_postgres() else "sqlite",
        "db_ok": db_ok,
    }
