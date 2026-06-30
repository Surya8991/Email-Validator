import asyncio
import csv as _csv
import io as _io
import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.schemas import ProviderResult

logger = logging.getLogger(__name__)

_BASE = "https://api.bouncify.io/v1"

_STATUS_MAP = {
    "deliverable": "valid",
    "undeliverable": "invalid",
    "accept_all": "risky",
    "accept-all": "risky",  # bulk CSV download uses the hyphen variant
    "unknown": "unknown",
    "risky": "risky",
}


def _map(result: str) -> str:
    return _STATUS_MAP.get(result.lower().strip().strip('"'), "unknown")


def _yn(row: dict[str, Any], key: str) -> bool:
    """Bouncify's CSV uses Y/N for booleans."""
    return (row.get(key, "") or "").strip().strip('"').upper() == "Y"


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
        """Bulk-verify emails via Bouncify's bulk API.

        Five-step flow per their docs (https://bouncify.readme.io):
          1. POST /v1/bulk?apikey=KEY  body={auto_verify, emails: [{email}, ...]}
          2. (auto_verify=true skips the separate PATCH-to-start step)
          3. GET  /v1/bulk?apikey=KEY&job_id=ID  -> poll status until "completed"
          4. POST /v1/download?apikey=KEY&jobId=ID  body={filterResult: [...]}
             -> returns CSV with header `Email,Verification Result,...`
          5. Map CSV rows back to input order.

        Any uncaught exception falls back to per-email verify() via gather —
        the worker also runs a >50%-unknown defensive re-verify on top of
        this, so a partial failure here still recovers.
        """
        if not self._key:
            return [ProviderResult(status="unknown", sub_status="no_api_key") for _ in emails]
        if not emails:
            return []

        try:
            # 1. Upload + auto-verify in one shot.
            upload_resp = await self._client.post(
                f"{_BASE}/bulk",
                params={"apikey": self._key},
                json={
                    "auto_verify": True,
                    "emails": [{"email": e} for e in emails],
                },
                timeout=30.0,
            )
            upload_resp.raise_for_status()
            upload_data = upload_resp.json()
            job_id = upload_data.get("job_id")
            if not job_id:
                raise RuntimeError(f"bouncify upload returned no job_id: {upload_data!r}")

            # 2. Poll status. Bouncify's status field uses values:
            #    preparing | ready | verifying | completed | failed | cancelled
            terminal_failure = {"failed", "cancelled"}
            for _ in range(180):  # 180 * 5s = 15 min hard ceiling
                await asyncio.sleep(5)
                st = await self._client.get(
                    f"{_BASE}/bulk",
                    params={"apikey": self._key, "job_id": job_id},
                    timeout=15.0,
                )
                st.raise_for_status()
                st_data = st.json()
                status = (st_data.get("status") or "").lower()
                if status == "completed":
                    break
                if status in terminal_failure:
                    raise RuntimeError(
                        f"bouncify job {job_id} {status}: {st_data!r}"
                    )
            else:
                raise TimeoutError(f"bouncify job {job_id} did not complete within 15 min")

            # 3. Download CSV (POST, not GET; jobId not job_id in this endpoint).
            dl = await self._client.post(
                f"{_BASE}/download",
                params={"apikey": self._key, "jobId": job_id},
                json={
                    "filterResult": [
                        "deliverable", "undeliverable", "accept_all", "unknown",
                    ],
                },
                timeout=60.0,
            )
            dl.raise_for_status()
            csv_text = dl.text or ""
            if not csv_text.strip():
                raise RuntimeError(f"bouncify download returned empty body for job {job_id}")

            # 4. Parse CSV. Header (per docs):
            #    Email, Verification Result, Syntax Error, ISP, Role,
            #    Disposable, Trap, Verified At
            reader = _csv.DictReader(_io.StringIO(csv_text))
            row_by_email: dict[str, dict[str, Any]] = {}
            for row in reader:
                key = (row.get("Email") or row.get("email") or "").strip().strip('"').lower()
                if key:
                    row_by_email[key] = row

            # 5. Build per-email ProviderResult preserving input order.
            results: list[ProviderResult] = []
            for e in emails:
                row = row_by_email.get(e.strip().lower())  # type: ignore[assignment]
                if not row:
                    results.append(ProviderResult(
                        status="unknown",
                        sub_status="missing_in_bulk_response",
                        raw={},
                    ))
                    continue
                raw_result = (
                    row.get("Verification Result")
                    or row.get("verification_result")
                    or ""
                ).strip().strip('"')
                results.append(ProviderResult(
                    status=_map(raw_result),
                    sub_status=raw_result,
                    is_disposable=_yn(row, "Disposable"),
                    is_role=_yn(row, "Role"),
                    is_free=_yn(row, "ISP"),
                    mx_found=True,
                    raw=row,
                ))
            return results
        except Exception as e:
            logger.warning(
                "bouncify.verify_bulk failed (%s) — falling back to per-email gather",
                e,
            )
            results = await asyncio.gather(*[self.verify(em) for em in emails])
            return list(results)
