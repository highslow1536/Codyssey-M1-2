import time

import firebase_admin
import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.firebase import get_firebase_app, get_firestore
from app.main import app

pytestmark = pytest.mark.integration


def test_firebase_admin_validates_emulator_claims_without_auth_override(
    firestore_db, owner_uid, monkeypatch
):
    # Firebase explicitly accepts unsigned tokens ONLY with its emulator flag.
    # This exercises the real Admin SDK claim checks; no auth function is mocked.
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo-stock-assistant")
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    monkeypatch.setenv("ALLOWED_USER_UIDS", owner_uid)
    get_settings.cache_clear()
    get_firestore.cache_clear()
    get_firebase_app.cache_clear()
    now = int(time.time())
    claims = {
        "aud": "demo-stock-assistant",
        "iss": "https://securetoken.google.com/demo-stock-assistant",
        "sub": owner_uid,
        "iat": now,
        "exp": now + 3600,
        "auth_time": now,
        "firebase": {"identities": {}, "sign_in_provider": "custom"},
    }
    token = jwt.encode(claims, key="", algorithm="none")
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            assert client.get("/api/instruments", headers=headers).status_code == 200
            summary = client.get("/api/data/summary", headers=headers)
            assert summary.status_code == 200, summary.text
            assert summary.json()["count"] == 0
            bad = jwt.encode({**claims, "aud": "another-project"}, key="", algorithm="none")
            assert (
                client.get(
                    "/api/instruments", headers={"Authorization": f"Bearer {bad}"}
                ).status_code
                == 401
            )
    finally:
        firebase_admin.delete_app(get_firebase_app())
        get_firebase_app.cache_clear()
        get_firestore.cache_clear()
        get_settings.cache_clear()
