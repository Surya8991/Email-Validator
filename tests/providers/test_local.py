from unittest.mock import patch

import pytest

from app.providers.local import LocalProvider


@pytest.mark.asyncio
async def test_valid_email():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value=True):
        result = await provider.verify("user@example.com")
    assert result.status == "valid"
    assert result.mx_found is True


@pytest.mark.asyncio
async def test_disposable_email():
    provider = LocalProvider()
    # mailinator.com is in disposable list
    result = await provider.verify("test@mailinator.com")
    assert result.status == "risky"
    assert result.is_disposable is True
    assert result.sub_status == "disposable"


@pytest.mark.asyncio
async def test_no_mx():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value=False):
        result = await provider.verify("user@no-such-domain-xyz.com")
    assert result.status == "invalid"
    assert result.sub_status == "no_mx"


@pytest.mark.asyncio
async def test_syntax_error():
    provider = LocalProvider()
    result = await provider.verify("not-an-email")
    assert result.status == "invalid"
    assert result.sub_status == "syntax_error"


@pytest.mark.asyncio
async def test_role_address():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value=True):
        result = await provider.verify("info@example.com")
    assert result.is_role is True
    assert result.sub_status == "role_based"


@pytest.mark.asyncio
async def test_free_provider():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value=True):
        result = await provider.verify("user@gmail.com")
    assert result.is_free is True


@pytest.mark.asyncio
async def test_bulk():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value=True):
        results = await provider.verify_bulk(["user@example.com", "bad-email"])
    assert len(results) == 2
    assert results[0].status == "valid"
    assert results[1].status == "invalid"
