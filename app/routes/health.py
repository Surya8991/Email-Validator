from fastapi import APIRouter

from app.db import is_postgres
from app.providers.registry import get_enabled_providers

router = APIRouter()


@router.get("/api/health")
async def health():
    return {
        "status": "ok",
        "providers_enabled": get_enabled_providers(),
        "database": "postgresql" if is_postgres() else "sqlite",
    }
