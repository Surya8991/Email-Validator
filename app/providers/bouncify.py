import asyncio

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.schemas import ProviderResult

_BASE = "https://api.bouncify.io/v1"

_STATUS_MAP = {
    "deliverable": "valid",
    "undeliverable": "invalid",
    "accept_all": "risky",
    "unknown": "unknown",
    "risky": "risky",
}


def _map(result: str) -> str:
    return _STATUS_MAP.get(result.lower(), "unknown")


class BouncifyProvider:
    name = "bouncify"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._key = settings.bouncify_api_key

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    async def verify(self, email: str) -> ProviderResult:
        if not self._key:
            return ProviderResult(
                status="unknown", sub_status="no_api_key", error="BOUNCIFY_API_KEY not set"
            )
        try:
            resp = await self._client.get(
                f"{_BASE}/verify",
                params={"apikey": self._key, "email": email},
                timeout=15.0,
            )
            if resp.status_code == 429:
                return ProviderResult(
                    status="unknown", sub_status="rate_limited", error="rate limited"
                )
            resp.raise_for_status()
            data = resp.json()
            status = _map(data.get("result", "unknown"))
            return ProviderResult(
                status=status,
                sub_status=data.get("sub_status", ""),
                is_disposable=data.get("is_disposable_address", False),
                is_role=data.get("is_role_address", False),
                is_free=data.get("is_free_email", False),
                mx_found=data.get("mx_found", True),
                raw=data,
            )
        except httpx.HTTPStatusError as e:
            return ProviderResult(status="unknown", sub_status="http_error", error=str(e))
        except Exception as e:
            return ProviderResult(status="unknown", sub_status="error", error=str(e))

    async def verify_bulk(self, emails: list[str]) -> list[ProviderResult]:
        # Bouncify bulk: create job → poll → download
        if not self._key:
            return [ProviderResult(status="unknown", sub_status="no_api_key") for _ in emails]

        try:
            # Create bulk job
            resp = await self._client.post(
                f"{_BASE}/bulk",
                json={"apikey": self._key, "emails": emails},
                timeout=30.0,
            )
            resp.raise_for_status()
            job_data = resp.json()
            job_id = job_data.get("id") or job_data.get("job_id")

            if not job_id:
                # Fallback to individual calls if bulk not available
                results = await asyncio.gather(*[self.verify(e) for e in emails])
                return list(results)

            # Poll until done
            for _ in range(60):  # max 5 min
                await asyncio.sleep(5)
                status_resp = await self._client.get(
                    f"{_BASE}/bulk/{job_id}",
                    params={"apikey": self._key},
                    timeout=15.0,
                )
                status_resp.raise_for_status()
                status_data = status_resp.json()
                if status_data.get("status") in ("completed", "done"):
                    break

            # Download results
            dl_resp = await self._client.get(
                f"{_BASE}/download/{job_id}",
                params={"apikey": self._key},
                timeout=30.0,
            )
            dl_resp.raise_for_status()
            ct = dl_resp.headers.get("content-type", "")
            rows = dl_resp.json() if ct.startswith("application/json") else []

            if isinstance(rows, list):
                result_map = {r.get("email", "").lower(): r for r in rows}
                return [
                    ProviderResult(
                        status=_map(result_map.get(e.lower(), {}).get("result", "unknown")),
                        sub_status=result_map.get(e.lower(), {}).get("sub_status", ""),
                        raw=result_map.get(e.lower(), {}),
                    )
                    for e in emails
                ]

        except Exception:
            pass

        # Fallback
        results = await asyncio.gather(*[self.verify(e) for e in emails])
        return list(results)
