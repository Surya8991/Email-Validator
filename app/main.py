from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
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


app = FastAPI(
    title="Email Validator",
    version="0.3.0",
    lifespan=lifespan,
    docs_url=None,  # custom /docs with back button below
)

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
if _SAMPLES_DIR.is_dir():
    app.mount("/samples", StaticFiles(directory=str(_SAMPLES_DIR)), name="samples")

app.include_router(health.router)
app.include_router(api_single.router)
app.include_router(api_bulk.router)
app.include_router(api_stats.router)
app.include_router(ui.router)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_docs() -> HTMLResponse:
    html = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="Email Validator — API Reference",
    )
    back_btn = (
        '<div style="position:fixed;top:14px;left:14px;z-index:9999">'
        '<a href="/" style="display:inline-flex;align-items:center;gap:6px;'
        "background:#4f46e5;color:#fff;text-decoration:none;"
        "padding:7px 16px;border-radius:8px;font-family:ui-sans-serif,system-ui,sans-serif;"
        'font-size:13px;font-weight:600;box-shadow:0 2px 8px rgba(79,70,229,.4)">'
        "← Back to App"
        "</a>"
        "</div>"
    )
    content = html.body.decode("utf-8").replace("<body>", f"<body>{back_btn}", 1)
    return HTMLResponse(content=content)
