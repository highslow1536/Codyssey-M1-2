"""Unit tests for the CNN Fear & Greed provider (mocked HTTP, no network)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.errors import AppError
from app.providers.cnn import ENDPOINT_URL, SOURCE_URL, CNNProvider

NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
TS = int(datetime(2026, 1, 2, 11, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _payload(**overrides):
    base = {
        "fear_and_greed": {
            "score": 42.0,
            "rating": "Fear",
            "timestamp": "2026-01-02T11:00:00Z",
            "previous_close": 40.0,
            "previous_1_week": {"score": 38.0},
            "previous_1_month": {"score": 50.0},
            "previous_1_year": {"score": 60.0},
        },
        "fear_and_greed_historical": {
            "timestamp": float(TS),
            "score": 42.0,
            "rating": "Fear",
            "data": [
                {"x": float(TS - 86400000), "y": 40.0, "rating": "Fear"},
                {"x": float(TS), "y": 42.0, "rating": "Fear"},
                {"x": float(TS), "y": 43.0, "rating": "Neutral"},
            ],
        },
    }
    base.update(overrides)
    return base


def _client_for(payload, status=200, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["headers"] = dict(request.headers)
            capture["url"] = str(request.url)
        return httpx.Response(status_code=status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_success_shape_and_dedup_sort():
    capture: dict = {}
    provider = CNNProvider(client=_client_for(_payload(), capture=capture))
    out = provider.fetch(now=NOW)
    assert out["provider"] == "cnn"
    assert out["market"] == "US"
    assert out["value"] == 42.0
    assert out["rating"] == "Fear"
    assert out["event_time"].tzinfo is not None
    assert out["fetched_at"] == NOW
    assert out["source_url"] == SOURCE_URL
    assert out["endpoint_url"] == ENDPOINT_URL
    assert out["previous_close"] == 40.0
    assert len(out["history"]) == 2
    assert out["history"][0]["event_time"] < out["history"][1]["event_time"]
    assert out["history"][-1]["value"] == 43.0
    low = {k.lower(): v for k, v in capture["headers"].items()}
    assert "user-agent" in low
    assert "accept" in low
    assert capture["url"] == ENDPOINT_URL


def test_future_main_timestamp_rejected():
    payload = _payload()
    payload["fear_and_greed"]["timestamp"] = "2026-01-02T12:10:00Z"
    provider = CNNProvider(client=_client_for(payload))
    with pytest.raises(AppError) as exc:
        provider.fetch(now=NOW)
    assert exc.value.code == "cnn_payload_invalid"
    assert exc.value.status_code == 502


def test_invalid_score_and_timestamp_raise_502():
    for bad in ("not-a-number", -1, 101, None, ""):
        payload = _payload()
        payload["fear_and_greed"]["score"] = bad
        with pytest.raises(AppError) as exc:
            CNNProvider(client=_client_for(payload)).fetch(now=NOW)
        assert exc.value.code == "cnn_payload_invalid"
    raw = httpx.Response(
        status_code=200,
        content=b'{"fear_and_greed": {"score": NaN, "rating": "Fear", "timestamp": "2026-01-02T11:00:00Z"}}',
    )
    with pytest.raises(AppError) as exc:
        CNNProvider(client=httpx.Client(transport=httpx.MockTransport(lambda req: raw))).fetch(
            now=NOW
        )
    assert exc.value.code == "cnn_payload_invalid"
    payload = _payload()
    payload["fear_and_greed"]["timestamp"] = "not-a-time"
    with pytest.raises(AppError) as exc:
        CNNProvider(client=_client_for(payload)).fetch(now=NOW)
    assert exc.value.status_code == 502
    payload = _payload()
    payload["fear_and_greed"]["rating"] = "   "
    with pytest.raises(AppError) as exc:
        CNNProvider(client=_client_for(payload)).fetch(now=NOW)
    assert exc.value.code == "cnn_payload_invalid"


def test_status_errors_without_body_and_rate_limit_code():
    for status, code in (
        (403, "cnn_unavailable"),
        (429, "cnn_rate_limited"),
        (500, "cnn_unavailable"),
    ):
        provider = CNNProvider(client=_client_for({"secret": "body"}, status=status))
        with pytest.raises(AppError) as exc:
            provider.fetch(now=NOW)
        assert exc.value.status_code == 503
        assert exc.value.code == code
        assert "secret" not in str(exc.value)
        assert "secret" not in exc.value.message


def test_malformed_comparison_null_with_notes_and_history_filtered():
    payload = _payload()
    payload["fear_and_greed"]["previous_close"] = {"score": "bad"}
    payload["fear_and_greed_historical"]["data"].extend(
        [
            {"x": "bad", "y": 10.0},
            {"x": TS - 1000, "y": 200.0},
            {"x": TS - 2000, "y": "nan"},
            "not-a-dict",
        ]
    )
    out = CNNProvider(client=_client_for(payload)).fetch(now=NOW)
    assert out["previous_close"] is None
    assert any("previous_close" in n for n in out["notes"])
    assert all(0 <= h["value"] <= 100 for h in out["history"])
    assert all(h["event_time"].tzinfo is not None for h in out["history"])


def test_stale_history_returned_not_rejected():
    old_ts = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    payload = _payload()
    payload["fear_and_greed_historical"] = {
        "data": [{"x": old_ts, "y": 25.0, "rating": "Extreme Fear"}]
    }
    out = CNNProvider(client=_client_for(payload)).fetch(now=NOW)
    assert len(out["history"]) == 1
    assert out["history"][0]["value"] == 25.0
    assert out["history"][0]["rating"] == "Extreme Fear"


def test_headers_use_app_user_agent_and_accept_json():
    capture: dict = {}
    CNNProvider(client=_client_for(_payload(), capture=capture)).fetch(now=NOW)
    low2 = {k.lower(): v for k, v in capture["headers"].items()}
    ua = low2.get("user-agent", "")
    assert "stock-market" in ua or "assistant" in ua.lower()
    assert "application/json" in low2.get("accept", "")


def test_legacy_historical_key_fallback():
    payload = _payload()
    hist = payload.pop("fear_and_greed_historical")
    payload["historical"] = hist
    out = CNNProvider(client=_client_for(payload)).fetch(now=NOW)
    assert len(out["history"]) == 2
    assert out["history"][-1]["value"] == 43.0


def test_history_capped_to_latest_366():
    base = TS - 400 * 86400000
    payload = _payload()
    payload["fear_and_greed_historical"] = {
        "data": [
            {"x": float(base + i * 86400000), "y": float(10 + (i % 80)), "rating": "Fear"}
            for i in range(400)
        ]
    }
    out = CNNProvider(client=_client_for(payload)).fetch(now=NOW)
    assert len(out["history"]) == 366
    assert out["history"][0]["value"] == float(10 + (34 % 80))
    assert any("366" in n for n in out["notes"])
