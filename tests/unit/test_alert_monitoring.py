from datetime import datetime, timezone

from app.catalog import get_instrument
from app.services.alerts import monitoring_window, preview


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_korea_waits_for_first_complete_bar_and_keeps_delayed_closing_tail():
    instrument = get_instrument("KRX_005930")
    assert not monitoring_window(instrument, at("2026-10-22T00:04:59"), 5)
    assert monitoring_window(instrument, at("2026-10-22T00:05:00"), 5)
    assert monitoring_window(instrument, at("2026-10-22T06:50:00"), 5)
    assert not monitoring_window(instrument, at("2026-10-22T07:03:00"), 5)
    assert not monitoring_window(instrument, at("2026-10-24T01:00:00"), 5)


def test_us_calendar_respects_holidays_early_close_and_dst():
    instrument = get_instrument("NASDAQ_AAPL")
    assert not monitoring_window(instrument, at("2026-11-26T16:00:00"), 5)
    assert monitoring_window(instrument, at("2026-11-27T17:50:00"), 5)
    assert not monitoring_window(instrument, at("2026-11-27T18:20:00"), 5)
    assert monitoring_window(instrument, at("2026-10-22T13:35:00"), 5)
    assert not monitoring_window(instrument, at("2026-11-02T13:35:00"), 5)
    assert monitoring_window(instrument, at("2026-11-02T14:35:00"), 5)


def test_preview_explains_sequence_decimal_threshold_and_observation_limits():
    rule = {
        "instrument_id": "KRX_005930",
        "currency": "KRW",
        "steps": [
            {"op": "cross_above", "price": "35000"},
            {"all": [{"op": "lte", "price": "30000"}, {"op": "gt", "price": "25000"}]},
        ],
    }
    result = preview(rule)
    assert "이후 관측" in result["description"]
    assert "그리고" in result["description"]
    assert result["observation"]["delay_seconds"] == 1200
    assert result["warnings"]
