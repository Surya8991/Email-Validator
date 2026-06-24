from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import create_db_tables
from app.providers import registry
from app.routes import api_bulk, api_single, api_stats, health, ui

_BASE_DIR = Path(__file__).parent.parent
_STATIC_DIR = _BASE_DIR / "static"
_SAMPLES_DIR = _BASE_DIR / "samples"


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_tables()
    registry._client = httpx.AsyncClient(timeout=settings.httpx_timeout)
    yield
    if registry._client and not registry._client.is_closed:
        await registry._client.aclose()


app = FastAPI(title="Email Validator", version="0.3.0", lifespan=lifespan)

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
if _SAMPLES_DIR.is_dir():
    app.mount("/samples", StaticFiles(directory=str(_SAMPLES_DIR)), name="samples")

app.include_router(health.router)
app.include_router(api_single.router)
app.include_router(api_bulk.router)
app.include_router(api_stats.router)
app.include_router(ui.router)
