from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ProviderResult(BaseModel):
    status: str  # valid | invalid | risky | unknown
    sub_status: str = ""
    is_disposable: bool = False
    is_role: bool = False
    is_free: bool = False
    mx_found: bool = True
    raw: dict[str, Any] = {}
    error: str | None = None
    # Local-provider extras — other providers leave these at defaults
    score: int | None = None  # 0-100, see local_rules.SCORE_WEIGHTS
    reason_codes: list[str] = []  # machine-readable, e.g. ["TYPO_DOMAIN"]
    canonical: str | None = None  # same-mailbox form for dedup
    suggestion: str | None = None  # full corrected email on suspected typo


class SingleVerifyRequest(BaseModel):
    email: str
    providers: list[str] = ["bouncify"]
    strategy: str = "bouncify_only"
    cache_ttl_days: int | None = None  # None = use global default


class SingleVerifyResponse(BaseModel):
    email: str
    verdict: str
    providers: dict[str, ProviderResult]
    elapsed_ms: float
    cached: bool = False
    cached_at: datetime | None = None
    expires_at: datetime | None = None
    confidence: int = 0


class BulkJobResponse(BaseModel):
    job_id: int
    total: int
    status: str


class BulkStatusResponse(BaseModel):
    job_id: int
    status: str
    progress: int
    total: int
    summary: dict[str, int]
    download_url: str | None = None
