from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session

from app.models import EmailCache
from app.schemas import ProviderResult


@pytest.fixture(autouse=True)
def use_test_db(patch_db):
    """Ensure tests use the in-memory DB already patched by conftest."""
    pass


def _make_result(status: str = "valid") -> ProviderResult:
    return ProviderResult(status=status, sub_status="")


def test_cache_miss_then_hit():
    from app.core.cache import get_cached, parse_cached_providers, set_cache

    email = "cachetest@example.com"
    assert get_cached(email) is None  # cold miss

    providers = {"bouncify": _make_result("valid")}
    set_cache(email, "valid", providers, "bouncify_only")

    row = get_cached(email)
    assert row is not None
    assert row.verdict == "valid"
    assert row.email == email

    parsed = parse_cached_providers(row)
    assert parsed["bouncify"].status == "valid"


def test_cache_case_insensitive():
    from app.core.cache import get_cached, set_cache

    set_cache("Upper@Example.COM", "valid", {"local": _make_result()}, "bouncify_only")
    row = get_cached("upper@example.com")
    assert row is not None


def test_expired_cache_returns_none():
    from app.core import cache
    from app.db import engine

    email = "expired@example.com"
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    with Session(engine) as session:
        session.add(EmailCache(
            email=email,
            verdict="valid",
            provider_data="{}",
            providers_used="local",
            strategy="bouncify_only",
            validated_at=past - timedelta(days=30),
            expires_at=past,
        ))
        session.commit()

    result = cache.get_cached(email)
    assert result is None  # expired → deleted


def test_cache_upsert():
    from app.core.cache import get_cached, set_cache

    email = "upsert@example.com"
    set_cache(email, "valid", {"local": _make_result("valid")}, "bouncify_only")
    set_cache(email, "invalid", {"bouncify": _make_result("invalid")}, "bouncify_only")

    row = get_cached(email)
    assert row is not None
    assert row.verdict == "invalid"  # overwritten


def test_unknown_verdict_not_cached():
    from app.core.cache import get_cached
    from app.core.validator import validate_with_cache

    email = "unknown@example.com"

    async def run():
        mock_result = ProviderResult(status="unknown", sub_status="error")
        with patch(
            "app.core.validator.validate", new_callable=AsyncMock
        ) as mock_validate:
            mock_validate.return_value = ("unknown", {"bouncify": mock_result})
            verdict, providers, cache_row = await validate_with_cache(
                email, ["bouncify"], "bouncify_only"
            )
        assert verdict == "unknown"
        assert cache_row is None
        assert get_cached(email) is None

    import asyncio
    asyncio.run(run())


def test_validate_with_cache_returns_hit_on_second_call():
    from app.core.cache import set_cache
    from app.core.validator import validate_with_cache

    email = "hit@example.com"
    set_cache(email, "valid", {"bouncify": _make_result("valid")}, "bouncify_only")

    async def run():
        verdict, providers, cache_row = await validate_with_cache(
            email, ["bouncify"], "bouncify_only"
        )
        assert verdict == "valid"
        assert cache_row is not None
        assert cache_row.email == email

    import asyncio
    asyncio.run(run())


def test_bulk_set_cache_invalid():
    from app.core.cache import bulk_set_cache_invalid, get_cached

    emails = ["bulk1@example.com", "Bulk2@Example.com", "bulk3@example.com"]
    synced = bulk_set_cache_invalid(emails, strategy="retry_unknowns_strikeout")

    assert synced == 3
    for email in emails:
        row = get_cached(email)
        assert row is not None
        assert row.verdict == "invalid"
        assert row.strategy == "retry_unknowns_strikeout"


def test_bulk_set_cache_invalid_upserts_existing():
    from app.core.cache import bulk_set_cache_invalid, get_cached, set_cache

    email = "bulk-upsert@example.com"
    set_cache(email, "valid", {"local": _make_result("valid")}, "bouncify_only")

    synced = bulk_set_cache_invalid([email])

    assert synced == 1
    row = get_cached(email)
    assert row is not None
    assert row.verdict == "invalid"  # overwritten by the bulk flip


def test_bulk_set_cache_invalid_empty_list_noop():
    from app.core.cache import bulk_set_cache_invalid

    assert bulk_set_cache_invalid([]) == 0


def test_purge_expired():
    from app.core.cache import purge_expired
    from app.db import engine

    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    with Session(engine) as session:
        session.add(EmailCache(
            email="old1@example.com",
            verdict="valid",
            provider_data="{}",
            providers_used="local",
            strategy="bouncify_only",
            validated_at=past,
            expires_at=past,
        ))
        session.add(EmailCache(
            email="old2@example.com",
            verdict="invalid",
            provider_data="{}",
            providers_used="local",
            strategy="bouncify_only",
            validated_at=past,
            expires_at=past,
        ))
        session.commit()

    deleted = purge_expired()
    assert deleted >= 2
