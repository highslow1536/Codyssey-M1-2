"""CNN Fear & Greed provider (unofficial website feed, no official API contract)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.errors import AppError

ENDPOINT_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
SOURCE_URL = "https://www.cnn.com/markets/fear-and-greed"
APP_USER_AGENT = "stock-market-assistant/0.1.0"
FUTURE_TOLERANCE_SECONDS = 5 * 60

__all__ = ["CNNProvider", "ENDPOINT_URL", "SOURCE_URL"]


def _to_aware_utc(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        num = float(value)
        if not math.isfinite(num):
            return None
        stamp = num / 1000.0 if abs(num) >= 1e11 else num
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return None
        try:
            num = float(text_value)
            if math.isfinite(num):
                stamp = num / 1000.0 if abs(num) >= 1e11 else num
                return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        try:
            dt = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _parse_score(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num < 0 or num > 100:
        return None
    return float(num)


def _extract_comparison(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("score", "value", "y"):
            if value.get(key) is not None:
                parsed = _parse_score(value.get(key))
                return parsed
        return None
    return _parse_score(value)


class CNNProvider:
    """Fetch the CNN Fear & Greed snapshot from the website data endpoint."""

    ENDPOINT_URL = ENDPOINT_URL
    SOURCE_URL = SOURCE_URL

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def fetch(self, *, now: datetime) -> dict:
        """Return normalized sentiment; stale history is returned, not fabricated."""
        if not isinstance(now, datetime):
            raise AppError(422, "invalid_now", "now must be a datetime.")
        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        now_utc = now_utc.astimezone(timezone.utc)
        headers = {
            "User-Agent": APP_USER_AGENT,
            "Accept": "application/json",
            "Referer": SOURCE_URL,
        }
        try:
            import httpx
        except ImportError as exc:
            raise AppError(503, "cnn_unavailable", "CNN data is temporarily unavailable.") from exc
        response = None
        try:
            if self._client is None:
                with httpx.Client(timeout=10.0) as owned:
                    response = owned.get(ENDPOINT_URL, headers=headers)
            else:
                response = self._client.get(ENDPOINT_URL, headers=headers, timeout=10.0)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(503, "cnn_unavailable", "CNN data is temporarily unavailable.") from exc
        status = getattr(response, "status_code", None)
        if status == 429:
            raise AppError(503, "cnn_rate_limited", "CNN data is rate limited.")
        if status != 200:
            raise AppError(503, "cnn_unavailable", "CNN data is temporarily unavailable.")
        try:
            payload = response.json()
        except Exception as exc:
            raise AppError(502, "cnn_payload_invalid", "CNN payload is invalid.") from exc
        if not isinstance(payload, dict):
            raise AppError(502, "cnn_payload_invalid", "CNN payload is invalid.")
        fear = payload.get("fear_and_greed")
        if not isinstance(fear, dict):
            raise AppError(502, "cnn_payload_invalid", "CNN payload is invalid.")
        raw_score = fear.get("score", fear.get("value", fear.get("y")))
        value = _parse_score(raw_score)
        if value is None:
            raise AppError(502, "cnn_payload_invalid", "CNN payload is invalid.")
        rating = fear.get("rating")
        if not isinstance(rating, str) or not rating.strip():
            raise AppError(502, "cnn_payload_invalid", "CNN payload is invalid.")
        rating = rating.strip()
        raw_ts = fear.get("timestamp", fear.get("date", fear.get("x")))
        event_time = _to_aware_utc(raw_ts)
        if event_time is None:
            raise AppError(502, "cnn_payload_invalid", "CNN payload is invalid.")
        if (event_time - now_utc).total_seconds() > FUTURE_TOLERANCE_SECONDS:
            raise AppError(502, "cnn_payload_invalid", "CNN payload is invalid.")
        notes: list[str] = []
        comparisons: dict[str, float | None] = {}
        for field in ("previous_close", "previous_1_week", "previous_1_month", "previous_1_year"):
            if field not in fear:
                comparisons[field] = None
                continue
            parsed = _extract_comparison(fear.get(field))
            if fear.get(field) is not None and parsed is None:
                notes.append(field + " is present but malformed; returned as null.")
            comparisons[field] = parsed
        historical = None
        for _key in ("fear_and_greed_historical", "historical", "historical_data"):
            if _key in payload:
                historical = payload[_key]
                break
        if historical is None:
            historical = {}
        raw_points: list[Any] = []
        if historical is None:
            raw_points = []
        elif isinstance(historical, dict):
            data = historical.get("data", historical.get("points", []))
            raw_points = data if isinstance(data, list) else []
            if not isinstance(data, list):
                notes.append("historical data is malformed; returned as empty.")
        elif isinstance(historical, list):
            raw_points = historical
        else:
            notes.append("historical data is malformed; returned as empty.")
        by_time: dict[datetime, dict] = {}
        dropped_history = 0
        for point in raw_points:
            if not isinstance(point, dict):
                dropped_history += 1
                continue
            raw_x = point.get("x", point.get("timestamp", point.get("date")))
            raw_y = point.get("y", point.get("score", point.get("value")))
            et = _to_aware_utc(raw_x)
            val = _parse_score(raw_y)
            if et is None or val is None:
                dropped_history += 1
                continue
            if (et - now_utc).total_seconds() > FUTURE_TOLERANCE_SECONDS:
                dropped_history += 1
                continue
            point_rating = point.get("rating")
            if point_rating is None:
                norm_rating = None
            elif isinstance(point_rating, str):
                norm_rating = point_rating.strip() or None
            else:
                norm_rating = None
            by_time[et] = {"event_time": et, "value": val, "rating": norm_rating}
        history = [by_time[k] for k in sorted(by_time.keys())]
        if len(history) > 366:
            history = history[-366:]
            notes.append("History capped to the latest 366 observations.")
        if dropped_history:
            notes.append("Dropped " + str(dropped_history) + " historical point(s) as invalid.")
        return {
            "provider": "cnn",
            "market": "US",
            "value": value,
            "rating": rating,
            "event_time": event_time,
            "fetched_at": now_utc,
            "source_url": SOURCE_URL,
            "endpoint_url": ENDPOINT_URL,
            "previous_close": comparisons["previous_close"],
            "previous_1_week": comparisons["previous_1_week"],
            "previous_1_month": comparisons["previous_1_month"],
            "previous_1_year": comparisons["previous_1_year"],
            "history": history,
            "notes": notes,
        }
