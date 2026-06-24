import asyncio

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.schemas import ProviderResult

_BASE = "https://api.zerobounce.net/v2"

_STATUS_MAP = {
    "valid": "valid",
    "invalid": "invalid",
    "catch-all": "risky",
    "spamtrap": "invalid",
    "abuse": "risky",
    "do_not_mail": "risky",
    "unknown": "unknown",
}


class ZeroBounceProvider:
    name = "zerobounce"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._key = settings.zerobounce_api_key

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    async def verify(self, email: str) -> ProviderResult:
        if not self._key:
            return ProviderResult(
                status="unknown", sub_status="no_api_key", error="ZEROBOUNCE_API_KEY not set"
            )
        try:
            resp = await self._client.get(
                f"{_BASE}/validate",
                params={"api_key": self._key, "email": email, "ip_address": ""},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_status = data.get("status", "unknown").lower()
            status = _STATUS_MAP.get(raw_status, "unknown")
            return ProviderResult(
                status=status,
                sub_status=data.get("sub_status", ""),
                is_disposable=data.get("disposable", False),
                is_role=data.get("role", False),
                is_free=data.get("free_email", False),
                mx_found=bool(data.get("mx_record")),
                raw=data,
            )
        except Exception as e:
            return ProviderResult(status="unknown", sub_status="error", error=str(e))

    async def verify_bulk(self, emails: list[str]) -> list[ProviderResult]:
        sem = asyncio.Semaphore(10)

        async def _bounded(email: str) -> ProviderResult:
            async with sem:
                return await self.verify(email)

        results = await asyncio.gather(*[_bounded(e) for e in emails])
        return list(results)
