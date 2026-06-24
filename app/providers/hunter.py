import asyncio

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.schemas import ProviderResult

_BASE = "https://api.hunter.io/v2"

_STATUS_MAP = {
    "valid": "valid",
    "invalid": "invalid",
    "accept_all": "risky",
    "webmail": "valid",
    "disposable": "risky",
    "unknown": "unknown",
}


class HunterProvider:
    name = "hunter"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._key = settings.hunter_api_key

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    async def verify(self, email: str) -> ProviderResult:
        if not self._key:
            return ProviderResult(
                status="unknown", sub_status="no_api_key", error="HUNTER_API_KEY not set"
            )
        try:
            resp = await self._client.get(
                f"{_BASE}/email-verifier",
                params={"email": email, "api_key": self._key},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            d = data.get("data", {})
            raw_status = d.get("status", "unknown").lower()
            status = _STATUS_MAP.get(raw_status, "unknown")
            is_free = raw_status == "webmail" or d.get("webmail", False)
            return ProviderResult(
                status=status,
                sub_status=raw_status,
                is_disposable=d.get("disposable", False),
                is_role=False,
                is_free=is_free,
                mx_found=bool(d.get("mx_records")),
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
