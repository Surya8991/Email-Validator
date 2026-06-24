from unittest.mock import patch

import httpx
import pytest
import respx

from app.providers.bouncify import BouncifyProvider


@pytest.fixture
def provider():
    client = httpx.AsyncClient()
    with patch("app.providers.bouncify.settings") as mock_settings:
        mock_settings.bouncify_api_key = "test-key"
        yield BouncifyProvider(client), client


@pytest.mark.asyncio
async def test_deliverable(provider):
    prov, client = provider
    with respx.mock:
        respx.get("https://api.bouncify.io/v1/verify").mock(
            return_value=httpx.Response(200, json={
                "email": "user@example.com",
                "result": "deliverable",
                "sub_status": "",
                "is_disposable_address": False,
                "is_role_address": False,
                "is_free_email": False,
                "mx_found": True,
            })
        )
        result = await prov.verify("user@example.com")
    assert result.status == "valid"
    assert result.mx_found is True
    await client.aclose()


@pytest.mark.asyncio
async def test_undeliverable(provider):
    prov, client = provider
    with respx.mock:
        respx.get("https://api.bouncify.io/v1/verify").mock(
            return_value=httpx.Response(200, json={
                "email": "bad@example.com",
                "result": "undeliverable",
                "sub_status": "mailbox_not_found",
                "is_disposable_address": False,
                "is_role_address": False,
                "is_free_email": False,
                "mx_found": True,
            })
        )
        result = await prov.verify("bad@example.com")
    assert result.status == "invalid"
    assert result.sub_status == "mailbox_not_found"
    await client.aclose()


@pytest.mark.asyncio
async def test_accept_all_maps_to_risky(provider):
    prov, client = provider
    with respx.mock:
        respx.get("https://api.bouncify.io/v1/verify").mock(
            return_value=httpx.Response(200, json={
                "email": "catch@example.com",
                "result": "accept_all",
                "sub_status": "catch_all",
                "is_disposable_address": False,
                "is_role_address": False,
                "is_free_email": False,
                "mx_found": True,
            })
        )
        result = await prov.verify("catch@example.com")
    assert result.status == "risky"
    await client.aclose()


@pytest.mark.asyncio
async def test_no_api_key():
    client = httpx.AsyncClient()
    with patch("app.providers.bouncify.settings") as mock_settings:
        mock_settings.bouncify_api_key = ""
        prov = BouncifyProvider(client)
        result = await prov.verify("user@example.com")
    assert result.status == "unknown"
    assert result.sub_status == "no_api_key"
    await client.aclose()
