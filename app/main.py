import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import RequiresAdmin, RequiresAuth, RequiresMaintenance
from app.config import settings
from app.db import backfill_team_owners, create_db_tables
from app.providers import registry
from app.routes import admin as admin_router
from app.routes import api_bulk, api_single, api_stats, auth_routes, health, ui

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent
_STATIC_DIR = _BASE_DIR / "static"
_SAMPLES_DIR = _BASE_DIR / "samples"


def _bootstrap_admin() -> None:
    import bcrypt
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import User

    with Session(engine) as db:
        # Create first admin if table is empty
        if settings.admin_email and settings.admin_password:
            if not db.exec(select(User)).first():
                pw_hash = bcrypt.hashpw(
                    settings.admin_password.encode(), bcrypt.gensalt(rounds=12)
                ).decode()
                db.add(User(
                    email=settings.admin_email.strip().lower(),
                    password_hash=pw_hash,
                    role="admin",
                    is_active=True,
                ))
                db.commit()

        # Promote/create superadmin on every startup
        if settings.superadmin_email:
            sa_email = settings.superadmin_email.strip().lower()
            user = db.exec(select(User).where(User.email == sa_email)).first()
            if user:
                if user.role != "superadmin":
                    user.role = "superadmin"
                    user.is_active = True
                    db.commit()
            elif settings.admin_password:
                pw_hash = bcrypt.hashpw(
                    settings.admin_password.encode(), bcrypt.gensalt(rounds=12)
                ).decode()
                db.add(User(
                    email=sa_email,
                    password_hash=pw_hash,
                    role="superadmin",
                    is_active=True,
                ))
                db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: skip heavy DB ops on Vercel (Hobby 10s cold-start budget).

    create_db_tables / _bootstrap_admin / backfill_team_owners are run by
    the db_init GitHub Actions workflow on every push to main instead.
    On local dev (no VERCEL env var) they still run at startup as before.
    """
    import asyncio
    import os

    if settings.production and settings.secret_key == "dev-secret-change-me-in-production":
        raise RuntimeError(
            "SECRET_KEY must be changed in production. Set the SECRET_KEY environment variable."
        )

    if not os.getenv("VERCEL"):
        async def _run(fn):
            try:
                await asyncio.to_thread(fn)
                return (fn.__name__, "ok", None)
            except Exception as e:  # noqa: BLE001
                return (fn.__name__, "error", repr(e))

        async def _pipeline():
            r1 = await _run(create_db_tables)
            r2 = await asyncio.gather(_run(_bootstrap_admin), _run(backfill_team_owners))
            return [r1, *r2]

        try:
            results = await asyncio.wait_for(_pipeline(), timeout=4.0)
            for name, status, err in results:
                if status != "ok":
                    logger.warning("[startup] %s %s: %s", name, status, err)
        except TimeoutError:
            logger.warning("[startup] DB ops exceeded 4s — continuing")

    registry._client = httpx.AsyncClient(timeout=settings.httpx_timeout)
    yield
    if registry._client and not registry._client.is_closed:
        await registry._client.aclose()


app = FastAPI(
    title="Email Validator",
    version="0.10.3",
    lifespan=lifespan,
    docs_url=None,  # custom /docs with back button below
)


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defense-in-depth response headers + Origin-based CSRF check.

    Origin check is a lightweight CSRF defence — for state-changing requests
    we require the Origin header to match the request host. samesite="lax"
    already blocks most cross-site POSTs; this catches the edge cases.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method not in _SAFE_METHODS:
            # Exempt machine-to-machine callbacks (GitHub Actions) from the
            # browser-origin check — they use a shared secret instead.
            _exempt = request.url.path.endswith("/workflow-callback")
            if not _exempt:
                origin = request.headers.get("origin") or request.headers.get("referer")
                if origin:
                    try:
                        origin_host = urlparse(origin).netloc
                    except ValueError:
                        origin_host = ""
                    host = request.headers.get("host", "")
                    if origin_host and host and origin_host != host:
                        return JSONResponse(
                            {"detail": "Cross-origin request blocked."},
                            status_code=403,
                        )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com "
            "https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self';"
        )
        if settings.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)

if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
if _SAMPLES_DIR.is_dir():
    app.mount("/samples", StaticFiles(directory=str(_SAMPLES_DIR)), name="samples")

@app.exception_handler(RequiresAuth)
async def _requires_auth(_: Request, __: RequiresAuth) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=302)


@app.exception_handler(RequiresAdmin)
async def _requires_admin(_: Request, __: RequiresAdmin) -> HTMLResponse:
    return HTMLResponse("<h1>403 — Admin access required</h1>", status_code=403)


_MAINTENANCE_HTML = (
    "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'/>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'/>"
    "<title>Maintenance — EmailValidator</title>"
    "<script src='https://cdn.tailwindcss.com'></script></head>"
    "<body class='min-h-screen bg-gray-50 flex items-center justify-center p-6'>"
    "<div class='max-w-md text-center'><div class='text-6xl mb-6'>🔧</div>"
    "<h1 class='text-2xl font-bold text-gray-900 mb-2'>Maintenance in progress</h1>"
    "<p class='text-gray-500'>We'll be back shortly. Admins can still sign in.</p>"
    "<a href='/login' class='mt-6 inline-block bg-indigo-600 text-white"
    " px-6 py-2.5 rounded-lg text-sm font-medium hover:bg-indigo-700'>Sign in</a>"
    "</div></body></html>"
)


@app.exception_handler(RequiresMaintenance)
async def _requires_maintenance(_: Request, __: RequiresMaintenance) -> HTMLResponse:
    return HTMLResponse(_MAINTENANCE_HTML, status_code=503)


app.include_router(auth_routes.router)
app.include_router(admin_router.router)
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
