"""Tiny in-memory per-IP rate limiter.

Serverless caveat: on Vercel each cold-start instance has its own counters,
so a true burst from many regions can still squeeze through. Use a shared
store (Redis) if you need a hard cap across regions. This limiter still
blocks the common case — a single attacker spraying one endpoint.
"""
from __future__ import annotations

import time
from collections import deque
from threading import Lock

from fastapi import HTTPException, Request

# (ip, scope) -> deque[timestamps]
_buckets: dict[tuple[str, str], deque[float]] = {}
_lock = Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def rate_limit(request: Request, scope: str, max_hits: int, window_seconds: int) -> None:
    """Raise 429 if the caller's IP has exceeded max_hits in the last window_seconds."""
    ip = _client_ip(request)
    key = (ip, scope)
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        bucket = _buckets.setdefault(key, deque(maxlen=max_hits + 1))
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_hits:
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests. Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
