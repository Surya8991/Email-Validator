import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlmodel import Session

from app.auth import get_current_user
from app.core.validator import validate_with_cache
from app.db import engine
from app.schemas import ProviderResult, SingleVerifyRequest, SingleVerifyResponse

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _confidence(verdict: str, providers: dict[str, ProviderResult]) -> int:
    if verdict == "unknown":
        return 20
    agreeing = sum(1 for p in providers.values() if p.status == verdict)
    total = max(1, len(providers))
    base = {"valid": 70, "invalid": 82, "risky": 50}.get(verdict, 25)
    return min(98, base + int((agreeing / total) * 20))


@router.post("/api/verify", response_model=SingleVerifyResponse)
async def verify_single_json(req: SingleVerifyRequest):
    t0 = time.monotonic()
    verdict, providers, cache_row = await validate_with_cache(
        req.email, req.providers, req.strategy, ttl_days=req.cache_ttl_days
    )
    elapsed = (time.monotonic() - t0) * 1000
    return SingleVerifyResponse(
        email=req.email,
        verdict=verdict,
        providers=providers,
        elapsed_ms=round(elapsed, 2),
        cached=cache_row is not None,
        cached_at=cache_row.validated_at if cache_row else None,
        expires_at=cache_row.expires_at if cache_row else None,
        confidence=_confidence(verdict, providers),
    )


@router.post("/verify/htmx", response_class=HTMLResponse)
async def verify_single_htmx(request: Request):
    form = await request.form()
    email = str(form.get("email", ""))
    providers_raw = form.getlist("providers")
    if not providers_raw:
        providers_raw = ["bouncify"]
    strategy = str(form.get("strategy", "bouncify_only"))
    ttl_raw = form.get("cache_ttl_days")
    try:
        ttl_days: int | None = int(ttl_raw) if ttl_raw else None
    except (ValueError, TypeError):
        ttl_days = None

    with Session(engine) as db:
        current_user = get_current_user(request, db)
        if current_user and current_user.validation_limit is not None:
            month_start = datetime.utcnow().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            count = db.execute(
                text(
                    "SELECT COUNT(*) FROM emailresult e "
                    "JOIN job j ON j.id = e.job_id "
                    "WHERE j.user_id = :uid AND e.created_at >= :ms"
                ),
                {"uid": current_user.id, "ms": month_start},
            ).scalar() or 0
            if count >= current_user.validation_limit:
                return templates.TemplateResponse(
                    request, "partials/single_result.html",
                    {"limit_exceeded": True, "limit": current_user.validation_limit},
                )

    t0 = time.monotonic()
    verdict, provider_results, cache_row = await validate_with_cache(
        email, providers_raw, strategy, ttl_days=ttl_days
    )
    elapsed = (time.monotonic() - t0) * 1000

    result = SingleVerifyResponse(
        email=email,
        verdict=verdict,
        providers=provider_results,
        elapsed_ms=round(elapsed, 2),
        cached=cache_row is not None,
        cached_at=cache_row.validated_at if cache_row else None,
        expires_at=cache_row.expires_at if cache_row else None,
        confidence=_confidence(verdict, provider_results),
    )
    return templates.TemplateResponse(
        request, "partials/single_result.html",
        {"result": result},
    )
