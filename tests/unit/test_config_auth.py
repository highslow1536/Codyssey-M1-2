from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.dependencies import get_current_uid
from app.main import app


def test_production_cannot_accept_emulator_tokens():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production", firebase_auth_emulator_host="localhost:9099")


def test_example_blank_secrets_are_not_credentials():
    settings = Settings(_env_file=None, firebase_service_account_json="", openai_api_key="")
    assert settings.firebase_service_account_json is None
    assert settings.openai_api_key is None


def test_health_docs_and_authentication_boundary():
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/docs").status_code == 200
        for endpoint in ("/api/data", "/api/conversations", "/api/instruments"):
            response = client.get(endpoint)
            assert response.status_code == 401
            assert response.json()["code"] == "unauthenticated"
            assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_valid_token_from_unlisted_user_is_denied(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_UIDS", "only-this-user")
    get_settings.cache_clear()
    try:
        with (
            patch("app.dependencies.get_firebase_app", return_value=object()),
            patch("app.dependencies.auth.verify_id_token", return_value={"uid": "another-user"}),
        ):
            with TestClient(app) as client:
                assert (
                    client.get(
                        "/api/instruments", headers={"Authorization": "Bearer test"}
                    ).status_code
                    == 403
                )
    finally:
        get_settings.cache_clear()


def test_extra_owner_and_invalid_value_rejected_before_storage():
    app.dependency_overrides[get_current_uid] = lambda: "test-owner"
    # Invalid inputs still require a DB dependency, injected solely in this test.
    from app.dependencies import get_db

    app.dependency_overrides[get_db] = lambda: object()
    try:
        with TestClient(app) as client:
            for value in (0, -1, "NaN", "Infinity"):
                result = client.post(
                    "/api/data",
                    json={
                        "instrument_id": "NASDAQ_AAPL",
                        "date": "2025-01-01",
                        "value": value,
                        "owner_uid": "foreign",
                    },
                )
                assert result.status_code == 422
                assert result.json()["detail"]
    finally:
        app.dependency_overrides.clear()
