import asyncio

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.schemas import ProviderResult

_BASE = "https://api.neverbounce.com/v4"

_STATUS_MAP = {
    "valid": "valid",
    "invalid": "invalid",
    "disposable": "risky",
    "catchall": "risky",
    "unknown": "unknown",
}


class NeverBounceProvider:
    name = "neverbounce"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._key = settings.neverbounce_api_key

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    async def verify(self, email: str) -> ProviderResult:
        if not self._key:
            return ProviderResult(
                status="unknown", sub_status="no_api_key", error="NEVERBOUNCE_API_KEY not set"
            )
        try:
            resp = await self._client.get(
                f"{_BASE}/single/check",
                params={"key": self._key, "email": email},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_result = data.get("result", "unknown").lower()
            status = _STATUS_MAP.get(raw_result, "unknown")
            flags = data.get("flags", [])
            return ProviderResult(
                status=status,
                sub_status=raw_result,
                is_disposable="is_disposable" in flags,
                is_role="is_role_address" in flags,
                is_free="is_free_email_host" in flags,
                mx_found="has_dns" in flags,
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
