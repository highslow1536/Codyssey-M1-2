"""Browser-test API only. Real Firebase emulators; model output alone is a test double.

Never imported by app.main or deployment. No production authentication override.
Run from repository root with PYTHONPATH=. .venv/bin/python frontend/tests/serve_api.py.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn


def main():
    if (
        os.environ.get("APP_ENV") != "test"
        or os.environ.get("FIREBASE_PROJECT_ID") != "demo-stock-assistant"
    ):
        raise RuntimeError("Browser tests require APP_ENV=test and demo-stock-assistant")
    if (
        os.environ.get("FIRESTORE_EMULATOR_HOST") != "127.0.0.1:8080"
        or os.environ.get("FIREBASE_AUTH_EMULATOR_HOST") != "127.0.0.1:9099"
    ):
        raise RuntimeError("Both local Firebase emulators are required")
    from app.catalog import get_instrument
    from app.firebase import get_firestore
    from app.main import app
    from app.repositories.market import MarketRepository

    uid = "browser-test-owner"
    # Create known disposable emulator accounts. These are not cloud credentials.
    for local_id, email in [
        (uid, "researcher@example.test"),
        ("browser-test-other", "other@example.test"),
    ]:
        response = httpx.post(
            "http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/projects/demo-stock-assistant/accounts",
            headers={"Authorization": "Bearer owner"},
            json={
                "localId": local_id,
                "email": email,
                "password": "browser-test-password",
                "emailVerified": True,
            },
        )
        duplicate = response.json().get("error", {}).get("message") in {
            "EMAIL_EXISTS",
            "DUPLICATE_LOCAL_ID",
        }
        if response.status_code != 200 and not duplicate:
            response.raise_for_status()
    root = Path(__file__).resolve().parents[2]
    fixture = json.loads((root / "tests/fixtures/yahoo_aapl_daily.json").read_text())
    bars = fixture["bars"]
    for bar in bars:
        for field in ("event_time", "fetched_at"):
            bar[field] = datetime.fromisoformat(bar[field])
    MarketRepository(get_firestore()).save_bars(uid, get_instrument("NASDAQ_AAPL"), bars)

    def model(**kwargs):
        # Explicit test-only deterministic text, never used as market data.
        return {
            "text": "브라우저 검증용 모델 응답입니다. 실제 모델 호출이 아닙니다.\n서버 요약과 대화 저장 연결을 확인했습니다.",
            "tool_trace": [],
        }

    with patch("app.services.chat.default_model_caller", return_value=model):
        uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
