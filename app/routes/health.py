from fastapi import APIRouter

from app.providers.registry import get_enabled_providers

router = APIRouter()


@router.get("/api/health")
async def health():
    return {"status": "ok", "providers_enabled": get_enabled_providers()}
