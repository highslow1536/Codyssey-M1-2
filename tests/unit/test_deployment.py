import json
import os
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from jobs.check_config import check_config


@pytest.fixture(scope="module")
def service_account():
    # Disposable test credential, generated locally. Never contacts Google.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        "type": "service_account",
        "project_id": "demo-deployment",
        "client_email": "test@demo-deployment.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
        "private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    }


def deployment_settings(service_account, **changes):
    values = {
        "app_env": "production",
        "firebase_project_id": "demo-deployment",
        "firebase_service_account_json": json.dumps(service_account),
        "allowed_user_uids": "owner,another",
        "allowed_origins": "https://assistant.vercel.app,https://assistant.example.com",
        "firestore_emulator_host": None,
        "firebase_auth_emulator_host": None,
    }
    return Settings(_env_file=None, **(values | changes))


def test_offline_api_accepts_certificate_and_warns_optional_features(service_account):
    result = check_config(deployment_settings(service_account), "api")
    assert result["ok"]
    assert result["warnings"] == [
        "OpenAI_chat_unavailable_news_uses_excerpts",
        "Naver_news_unavailable",
        "Finnhub_news_unavailable",
    ]
    assert service_account["private_key"] not in json.dumps(result)


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "*",
        "https://*.vercel.app",
        "http://assistant.vercel.app",
        "https://localhost",
        "https://app.localhost",
        "https://[::1]",
        "https://assistant.vercel.app/",
        "https://assistant.vercel.app/path",
        "https://user:password@assistant.vercel.app",
        "https://assistant.vercel.app?x=1",
        "https://assistant.vercel.app#fragment",
        "https://assistant.vercel.app:invalid",
        "https://@assistant.vercel.app",
        "https://assistant.vercel.app?",
        "https://assistant.vercel.app#",
        "https://assistant.vercel.app:",
    ],
)
def test_api_preflight_rejects_cors_that_will_not_match_browser_origin(service_account, origin):
    result = check_config(deployment_settings(service_account, allowed_origins=origin), "api")
    assert "ALLOWED_ORIGINS_must_be_exact_public_https_origins" in result["errors"]


def test_certificate_file_and_project_consistency(service_account, tmp_path):
    path = tmp_path / "credential.json"
    path.write_text(json.dumps(service_account))
    settings = deployment_settings(
        service_account,
        firebase_service_account_json=None,
        google_application_credentials=str(path),
    )
    assert check_config(settings, "api")["ok"]
    settings.firebase_project_id = "different-project"
    assert "Firebase_service_account_project_mismatch" in check_config(settings, "api")["errors"]


@pytest.mark.parametrize("raw", [None, "{invalid", "[]", '{"type":"service_account"}'])
def test_invalid_or_missing_certificate_is_a_safe_failure(service_account, raw):
    result = check_config(
        deployment_settings(service_account, firebase_service_account_json=raw), "api"
    )
    assert not result["ok"]
    assert any(e.startswith("Firebase_service_account_") for e in result["errors"])


def test_worker_requires_valid_destination_and_allowlisted_owner(service_account, monkeypatch):
    # Even a configured webhook must not make a network request during preflight.
    monkeypatch.setattr("httpx.Client.request", lambda *a, **kw: pytest.fail("Network forbidden"))
    settings = deployment_settings(service_account, allowed_origins="")
    result = check_config(settings, "worker")
    assert set(result["errors"]) == {
        "JOB_OWNER_UID_must_be_allowlisted",
        "DISCORD_WEBHOOK_URL_required_or_invalid",
    }
    settings.job_owner_uid = "owner"
    settings.discord_webhook_url = "https://discord.com/api/webhooks/123456789012345678/test-token"
    assert check_config(settings, "worker")["ok"]
    settings.job_owner_uid = "foreign"
    settings.discord_webhook_url = "https://example.com/secret"
    assert not check_config(settings, "worker")["ok"]


def test_cli_rejects_emulators_without_printing_secret_input(tmp_path):
    env = {
        **os.environ,
        "STOCK_ASSISTANT_ENV_FILE": str(tmp_path / "absent.env"),
        "APP_ENV": "production",
        "FIREBASE_AUTH_EMULATOR_HOST": "localhost:9099",
        "FIREBASE_SERVICE_ACCOUNT_JSON": "do-not-print-this-credential",
    }
    result = subprocess.run(
        [sys.executable, "-m", "jobs.check_config", "--component", "api"],
        env=env,
        capture_output=True,
        text=True,
        # Permit posix_spawn: forking the earlier emulator tests' active gRPC
        # threads emits native diagnostics before the new interpreter starts.
        close_fds=False,
    )
    assert result.returncode == 1
    assert not json.loads(result.stdout)["ok"]
    assert "do-not-print" not in result.stdout + result.stderr
    assert result.stderr == ""


def test_production_app_cors_and_auth_boundary(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.delenv("FIREBASE_AUTH_EMULATOR_HOST", raising=False)
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://assistant.vercel.app")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
            assert client.get("/api/instruments").status_code == 401
            headers = {
                "Origin": "https://assistant.vercel.app",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            }
            allowed = client.options("/api/chat", headers=headers)
            assert allowed.status_code == 200
            assert allowed.headers["access-control-allow-origin"] == headers["Origin"]
            headers["Origin"] = "https://foreign.vercel.app"
            denied = client.options("/api/chat", headers=headers)
            assert denied.status_code == 400
            assert "access-control-allow-origin" not in denied.headers
    finally:
        get_settings.cache_clear()
