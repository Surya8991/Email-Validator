import logging

import httpx
from fastapi import APIRouter
from sqlalchemy import text
from sqlmodel import Session

from app.config import settings
from app.db import engine, is_postgres
from app.providers.registry import get_enabled_providers

logger = logging.getLogger(__name__)
router = APIRouter()


async def _check_bouncify() -> bool:
    if not settings.bouncify_api_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            # Bouncify exposes a balance endpoint that requires the key but
            # doesn't consume credits — confirms the key is live.
            resp = await client.get(
                "https://api.bouncify.io/v1/credits",
                params={"apikey": settings.bouncify_api_key},
            )
        return resp.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.warning("bouncify health check failed: %s", e)
        return False


@router.get("/api/health")
async def health(deep: int = 0):
    # Trivial SELECT 1 so an external keep-warm ping wakes the DB too,
    # not just the Vercel function.
    db_ok = False
    try:
        with Session(engine) as db:
            db.exec(text("SELECT 1")).first()
        db_ok = True
    except Exception as e:  # noqa: BLE001
        logger.warning("health: db check failed: %s", e)

    payload: dict = {
        "status": "ok" if db_ok else "degraded",
        "providers_enabled": get_enabled_providers(),
        "database": "postgresql" if is_postgres() else "sqlite",
        "db_ok": db_ok,
    }
    if deep:
        bouncify_ok = await _check_bouncify()
        payload["bouncify_ok"] = bouncify_ok
        if not bouncify_ok and settings.bouncify_api_key:
            payload["status"] = "degraded"
    return payload
