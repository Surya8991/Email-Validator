import httpx

from app.config import settings
from app.providers.bouncify import BouncifyProvider
from app.providers.hunter import HunterProvider
from app.providers.local import LocalProvider
from app.providers.neverbounce import NeverBounceProvider
from app.providers.zerobounce import ZeroBounceProvider

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return the shared httpx client, creating one lazily if needed."""
    global _client
    if _client is None or _client.is_closed:
        from app.config import settings
        _client = httpx.AsyncClient(timeout=settings.httpx_timeout)
    return _client


def get_all_providers() -> dict:
    client = get_client()
    return {
        "local": LocalProvider(),
        "bouncify": BouncifyProvider(client),
        "zerobounce": ZeroBounceProvider(client),
        "neverbounce": NeverBounceProvider(client),
        "hunter": HunterProvider(client),
    }


def get_enabled_providers() -> list[str]:
    enabled = ["local"]
    if settings.bouncify_api_key:
        enabled.append("bouncify")
    if settings.zerobounce_api_key:
        enabled.append("zerobounce")
    if settings.neverbounce_api_key:
        enabled.append("neverbounce")
    if settings.hunter_api_key:
        enabled.append("hunter")
    return enabled
