"""
Email validation result cache.
- Cache key: normalized email (lowercase)
- TTL: configurable, default 30 days
- Storage: SQLite via SQLModel (same DB as jobs)
- On cache hit: return stored verdict + provider data, skip all API calls
- On cache miss: validate normally, then store result
"""
import json
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import EmailCache
from app.schemas import ProviderResult


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def get_cached(email: str) -> EmailCache | None:
    """Return a live cache entry for this email, or None if missing/expired."""
    key = email.strip().lower()
    with Session(engine) as session:
        row = session.exec(
            select(EmailCache).where(EmailCache.email == key)
        ).first()
        if row is None:
            return None
        if row.expires_at < _now():
            session.delete(row)
            session.commit()
            return None
        return row


def get_cached_many(emails: list[str]) -> dict[str, EmailCache]:
    """Batch cache lookup. Returns {normalized_email: EmailCache} for live rows.

    Used by the bulk worker to avoid N round-trips against Neon on every
    sub-batch. Expired rows are skipped (not deleted here — let the per-email
    `get_cached()` path lazy-delete them, which keeps this read-only and fast).
    """
    if not emails:
        return {}
    keys = list({e.strip().lower() for e in emails if e and e.strip()})
    if not keys:
        return {}
    now = _now()
    with Session(engine) as session:
        rows = session.exec(
            select(EmailCache).where(EmailCache.email.in_(keys))  # type: ignore[attr-defined]
        ).all()
    return {r.email: r for r in rows if r.expires_at >= now}


def set_cache(
    email: str,
    verdict: str,
    providers: dict[str, ProviderResult],
    strategy: str,
    ttl_days: int | None = None,
) -> None:
    """Upsert a cache entry for this email."""
    key = email.strip().lower()
    now = _now()
    effective_ttl = ttl_days if (ttl_days is not None and ttl_days > 0) else settings.cache_ttl_days
    expires = now + timedelta(days=effective_ttl)
    provider_data = json.dumps({n: r.model_dump() for n, r in providers.items()})
    providers_used = ",".join(providers.keys())

    with Session(engine) as session:
        existing = session.exec(
            select(EmailCache).where(EmailCache.email == key)
        ).first()
        if existing:
            existing.verdict = verdict
            existing.provider_data = provider_data
            existing.providers_used = providers_used
            existing.strategy = strategy
            existing.validated_at = now
            existing.expires_at = expires
            session.add(existing)
        else:
            session.add(EmailCache(
                email=key,
                verdict=verdict,
                provider_data=provider_data,
                providers_used=providers_used,
                strategy=strategy,
                validated_at=now,
                expires_at=expires,
            ))
        session.commit()


def parse_cached_providers(row: EmailCache) -> dict[str, ProviderResult]:
    """Deserialize stored provider JSON back into ProviderResult objects."""
    try:
        raw = json.loads(row.provider_data)
        return {name: ProviderResult(**data) for name, data in raw.items()}
    except Exception:
        return {}


def purge_expired() -> int:
    """Delete all expired cache entries. Returns count deleted."""
    now = _now()
    with Session(engine) as session:
        expired = session.exec(
            select(EmailCache).where(EmailCache.expires_at < now)
        ).all()
        count = len(expired)
        for row in expired:
            session.delete(row)
        session.commit()
    return count
