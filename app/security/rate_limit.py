"""Tiny in-memory per-IP rate limiter.

Serverless caveat: on Vercel each cold-start instance has its own counters,
so a true burst from many regions can still squeeze through. Use a shared
store (Redis) if you need a hard cap across regions. This limiter still
blocks the common case — a single attacker spraying one endpoint.
"""
from __future__ import annotations

import ipaddress
import time
from collections import deque
from threading import Lock

from fastapi import HTTPException, Request

# (ip, scope) -> deque[timestamps]
_buckets: dict[tuple[str, str], deque[float]] = {}
_lock = Lock()

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _PRIVATE_NETS)
    except ValueError:
        return False


def _client_ip(request: Request) -> str:
    """Extract the real client IP from X-Forwarded-For.

    Takes the rightmost non-private IP in the chain — the trusted proxy
    appends the real client address at the end, while the leftmost entry
    is attacker-controlled and must be ignored.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        for part in reversed([p.strip() for p in fwd.split(",")]):
            if part and not _is_private(part):
                return part
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
