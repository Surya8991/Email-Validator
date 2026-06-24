from unittest.mock import AsyncMock, patch

from app.schemas import ProviderResult


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert isinstance(data["providers_enabled"], list)


def test_single_verify_json(client):
    mock_result = ProviderResult(status="valid", sub_status="")
    # validate_with_cache returns (verdict, providers, cache_row)
    with patch(
        "app.routes.api_single.validate_with_cache", new_callable=AsyncMock
    ) as mock_validate:
        mock_validate.return_value = ("valid", {"local": mock_result}, None)
        resp = client.post("/api/verify", json={
            "email": "test@example.com",
            "providers": ["local"],
            "strategy": "bouncify_only",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "valid"
    assert data["email"] == "test@example.com"
    assert "local" in data["providers"]
    assert data["cached"] is False


def test_single_verify_json_cache_hit(client):
    from datetime import datetime

    from app.models import EmailCache

    mock_result = ProviderResult(status="valid", sub_status="")
    fake_cache = EmailCache(
        email="test@example.com",
        verdict="valid",
        provider_data="{}",
        providers_used="bouncify",
        strategy="bouncify_only",
        validated_at=datetime(2026, 1, 1, 12, 0),
        expires_at=datetime(2026, 2, 1, 12, 0),
    )
    with patch(
        "app.routes.api_single.validate_with_cache", new_callable=AsyncMock
    ) as mock_validate:
        mock_validate.return_value = ("valid", {"bouncify": mock_result}, fake_cache)
        resp = client.post("/api/verify", json={
            "email": "test@example.com",
            "providers": ["bouncify"],
            "strategy": "bouncify_only",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["cached"] is True
    assert data["cached_at"] is not None
    assert data["expires_at"] is not None


def test_single_verify_htmx(client):
    mock_result = ProviderResult(status="invalid", sub_status="no_mx")
    with patch(
        "app.routes.api_single.validate_with_cache", new_callable=AsyncMock
    ) as mock_validate:
        mock_validate.return_value = ("invalid", {"local": mock_result}, None)
        resp = client.post("/verify/htmx", data={
            "email": "bad@nxdomain.invalid",
            "providers": "local",
            "strategy": "bouncify_only",
        })
    assert resp.status_code == 200
    assert "invalid" in resp.text


def test_single_verify_htmx_shows_cached_badge(client):
    from datetime import datetime

    from app.models import EmailCache

    mock_result = ProviderResult(status="valid", sub_status="")
    fake_cache = EmailCache(
        email="cached@example.com",
        verdict="valid",
        provider_data="{}",
        providers_used="bouncify",
        strategy="bouncify_only",
        validated_at=datetime(2026, 1, 1),
        expires_at=datetime(2026, 2, 1),
    )
    with patch(
        "app.routes.api_single.validate_with_cache", new_callable=AsyncMock
    ) as mock_validate:
        mock_validate.return_value = ("valid", {"bouncify": mock_result}, fake_cache)
        resp = client.post("/verify/htmx", data={
            "email": "cached@example.com",
            "providers": "bouncify",
            "strategy": "bouncify_only",
        })
    assert resp.status_code == 200
    assert "cached" in resp.text
    assert "from cache" in resp.text


def test_index_page_redirects_without_auth(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


def test_index_page(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text


def test_jobs_page(auth_client):
    resp = auth_client.get("/jobs")
    assert resp.status_code == 200
