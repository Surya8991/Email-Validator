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


@pytest.mark.asyncio
async def test_verify_bulk_round_trip(provider):
    """Round-trip the 5-step bulk flow: upload + auto_verify, poll until
    completed, POST /download, parse CSV. Asserts verdict distribution
    matches the per-email path on the same inputs — regression net for
    the v0.10.1 bug where the parser silently returned mostly 'unknown'.
    """
    prov, client = provider
    emails = ["a@example.com", "b@example.com", "c@example.com"]
    csv_body = (
        '"Email","Verification Result","Syntax Error",'
        '"ISP","Role","Disposable","Trap","Verified At"\n'
        '"a@example.com","deliverable","N","Y","N","N","N","2026-06-29T00:00:00Z"\n'
        '"b@example.com","undeliverable","N","Y","N","N","N","2026-06-29T00:00:00Z"\n'
        '"c@example.com","accept-all","N","Y","N","N","N","2026-06-29T00:00:00Z"\n'
    )
    with respx.mock:
        # Upload + auto_verify (POST /v1/bulk)
        respx.post("https://api.bouncify.io/v1/bulk").mock(
            return_value=httpx.Response(200, json={
                "job_id": "test-job-id",
                "success": True,
                "message": "ok",
            })
        )
        # Status poll (GET /v1/bulk) — return completed on first try
        respx.get("https://api.bouncify.io/v1/bulk").mock(
            return_value=httpx.Response(200, json={
                "job_id": "test-job-id",
                "status": "completed",
                "total": 3,
                "verified": 3,
            })
        )
        # Download (POST /v1/download) returns CSV
        respx.post("https://api.bouncify.io/v1/download").mock(
            return_value=httpx.Response(
                200,
                text=csv_body,
                headers={"content-type": "text/csv"},
            )
        )
        results = await prov.verify_bulk(emails)

    assert len(results) == 3
    assert [r.status for r in results] == ["valid", "invalid", "risky"]
    assert results[0].sub_status == "deliverable"
    assert results[2].sub_status == "accept-all"
    await client.aclose()


@pytest.mark.asyncio
async def test_verify_bulk_missing_row_marks_unknown(provider):
    """A bulk download row that doesn't match an input email maps to
    'unknown' with sub_status='missing_in_bulk_response' — the worker's
    >50%-unknown defense relies on this to detect parser drift."""
    prov, client = provider
    emails = ["a@example.com", "ghost@example.com"]
    csv_body = (
        '"Email","Verification Result","Syntax Error",'
        '"ISP","Role","Disposable","Trap","Verified At"\n'
        '"a@example.com","deliverable","N","Y","N","N","N","2026-06-29T00:00:00Z"\n'
    )
    with respx.mock:
        respx.post("https://api.bouncify.io/v1/bulk").mock(
            return_value=httpx.Response(200, json={"job_id": "j", "success": True})
        )
        respx.get("https://api.bouncify.io/v1/bulk").mock(
            return_value=httpx.Response(200, json={"job_id": "j", "status": "completed"})
        )
        respx.post("https://api.bouncify.io/v1/download").mock(
            return_value=httpx.Response(200, text=csv_body, headers={"content-type": "text/csv"})
        )
        results = await prov.verify_bulk(emails)

    assert results[0].status == "valid"
    assert results[1].status == "unknown"
    assert results[1].sub_status == "missing_in_bulk_response"
    await client.aclose()
