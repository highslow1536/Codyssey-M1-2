"""Yahoo provider parsing tests with mocked upstream frames (no network)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.catalog import get_instrument
from app.errors import AppError
from app.providers.yahoo import YahooProvider

KR_ID = "KRX_005930"
START = date(2024, 1, 8)
END = date(2024, 1, 10)

_TICKER_SPEC = ["history", "history_metadata", "get_history_metadata"]


def _frame(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Adj Close": [r[5] for r in rows],
            "Volume": [r[6] for r in rows],
            "Dividends": [r[7] for r in rows],
            "Stock Splits": [r[8] for r in rows],
        },
        index=idx,
    )


def _mock_ticker(frame_or_exc, metadata=None):
    ticker = MagicMock(spec=_TICKER_SPEC)
    ticker.history_metadata = {} if metadata is None else dict(metadata)
    ticker.get_history_metadata.return_value = {} if metadata is None else dict(metadata)
    if isinstance(frame_or_exc, Exception):
        ticker.history.side_effect = frame_or_exc
    else:
        ticker.history.return_value = frame_or_exc
    return ticker


def test_parses_sorted_bars_and_preserves_adjusted():
    frame = _frame(
        [
            ("2024-01-10", 70.0, 72.0, 69.0, 71.0, 70.5, 1000, 0.0, 0.0),
            ("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0),
            ("2024-01-09", 65.0, 67.0, 64.0, 66.0, 65.5, 1500, 10.0, 0.0),
        ]
    )
    with patch("yfinance.Ticker", return_value=_mock_ticker(frame)):
        bars = YahooProvider().fetch_daily(get_instrument(KR_ID), START, END)
    assert [b["date"] for b in bars] == ["2024-01-08", "2024-01-09", "2024-01-10"]
    first = bars[0]
    assert first["instrument_id"] == KR_ID
    assert first["resolution"] == "1d"
    assert first["close"] == 61.0
    assert first["adjusted_close"] == 60.5
    assert first["volume"] == 2000
    assert first["provider"] == "yahoo"
    assert first["currency"] == "KRW"
    assert first["timezone"] == "Asia/Seoul"
    assert first["delay_seconds"] == 1200
    assert first["quality_status"] == "delayed"
    assert "005930.KS" in first["source_url"]
    assert first["event_time"].tzinfo is not None
    assert first["fetched_at"].tzinfo is not None
    assert bars[2]["dividends"] == 0.0
    assert bars[1]["dividends"] == 10.0


def test_history_called_unadjusted_with_actions_and_inclusive_end():
    frame = _frame([("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0)])
    ticker = _mock_ticker(frame)
    with patch("yfinance.Ticker", return_value=ticker):
        YahooProvider().fetch_daily(get_instrument(KR_ID), START, START)
    _, kwargs = ticker.history.call_args
    assert kwargs["auto_adjust"] is False
    assert kwargs["actions"] is True
    assert kwargs["start"] == "2024-01-08"
    assert kwargs["end"] == "2024-01-09"
    assert ticker.get_history_metadata.call_count <= 1


def test_iana_exchange_zone_takes_precedence_over_timezone_abbreviation():
    frame = _frame([("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0)])
    ticker = _mock_ticker(
        frame,
        {
            "symbol": "AAPL",
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
            "timezone": "EST",
        },
    )
    with patch("yfinance.Ticker", return_value=ticker):
        bars = YahooProvider().fetch_daily(get_instrument("NASDAQ_AAPL"), START, START)
    assert len(bars) == 1


def test_filters_still_open_session():
    frame = _frame(
        [
            ("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0),
            ("2024-01-09", 65.0, 67.0, 64.0, 66.0, 65.5, 1500, 0.0, 0.0),
        ]
    )
    with (
        patch("yfinance.Ticker", return_value=_mock_ticker(frame)),
        patch("app.providers.yahoo.complete_sessions", return_value=[date(2024, 1, 8)]),
    ):
        bars = YahooProvider().fetch_daily(
            get_instrument(KR_ID), date(2024, 1, 8), date(2024, 1, 9)
        )
    assert [b["date"] for b in bars] == ["2024-01-08"]


def test_drops_invalid_rows_without_faking():
    frame = _frame(
        [
            ("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0),
            ("2024-01-09", float("nan"), 67.0, 64.0, 66.0, 65.5, 1500, 0.0, 0.0),
            ("2024-01-10", -1.0, 67.0, 64.0, 66.0, 65.5, 1500, 0.0, 0.0),
        ]
    )
    with patch("yfinance.Ticker", return_value=_mock_ticker(frame)):
        bars = YahooProvider().fetch_daily(get_instrument(KR_ID), START, END)
    assert [b["date"] for b in bars] == ["2024-01-08"]


def test_missing_adj_close_column_raises_503():
    idx = pd.DatetimeIndex(["2024-01-08"])
    frame = pd.DataFrame(
        {"Open": [60.0], "High": [62.0], "Low": [59.0], "Close": [61.0], "Volume": [2000]},
        index=idx,
    )
    with patch("yfinance.Ticker", return_value=_mock_ticker(frame)):
        with pytest.raises(AppError) as exc:
            YahooProvider().fetch_daily(get_instrument(KR_ID), START, START)
    assert exc.value.status_code == 503
    assert exc.value.code == "provider_unavailable"


def test_ohlc_range_violation_dropped():
    frame = _frame(
        [
            ("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0),
            ("2024-01-09", 65.0, 65.5, 64.0, 66.0, 65.5, 1500, 0.0, 0.0),
            ("2024-01-10", 70.0, 69.0, 68.0, 68.5, 68.0, 1000, 0.0, 0.0),
        ]
    )
    with patch("yfinance.Ticker", return_value=_mock_ticker(frame)):
        bars = YahooProvider().fetch_daily(get_instrument(KR_ID), START, END)
    assert [b["date"] for b in bars] == ["2024-01-08"]


def test_duplicate_session_date_keeps_last():
    frame = _frame(
        [
            ("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0),
            ("2024-01-08", 61.0, 63.0, 60.0, 62.0, 61.5, 2500, 0.0, 0.0),
        ]
    )
    with patch("yfinance.Ticker", return_value=_mock_ticker(frame)):
        bars = YahooProvider().fetch_daily(get_instrument(KR_ID), START, START)
    assert len(bars) == 1
    assert bars[0]["date"] == "2024-01-08"
    assert bars[0]["close"] == 62.0
    assert bars[0]["adjusted_close"] == 61.5
    assert bars[0]["volume"] == 2500


def test_history_metadata_mismatch_raises_503():
    frame = _frame([("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0)])
    bad = {"symbol": "WRONG", "exchangeTimezoneName": "Asia/Seoul", "currency": "KRW"}
    with patch("yfinance.Ticker", return_value=_mock_ticker(frame, bad)):
        with pytest.raises(AppError) as exc:
            YahooProvider().fetch_daily(get_instrument(KR_ID), START, START)
    assert exc.value.status_code == 503
    assert exc.value.code == "provider_unavailable"


def test_history_metadata_currency_mismatch_raises_503():
    frame = _frame([("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0)])
    bad = {"symbol": "005930.KS", "exchangeTimezoneName": "Asia/Seoul", "currency": "USD"}
    with patch("yfinance.Ticker", return_value=_mock_ticker(frame, bad)):
        with pytest.raises(AppError) as exc:
            YahooProvider().fetch_daily(get_instrument(KR_ID), START, START)
    assert exc.value.status_code == 503
    assert exc.value.code == "provider_unavailable"


def test_history_metadata_match_passes():
    frame = _frame([("2024-01-08", 60.0, 62.0, 59.0, 61.0, 60.5, 2000, 0.0, 0.0)])
    good = {"symbol": "005930.KS", "exchangeTimezoneName": "Asia/Seoul", "currency": "KRW"}
    with patch("yfinance.Ticker", return_value=_mock_ticker(frame, good)):
        bars = YahooProvider().fetch_daily(get_instrument(KR_ID), START, START)
    assert [b["date"] for b in bars] == ["2024-01-08"]


def test_empty_frame_raises_503():
    with patch("yfinance.Ticker", return_value=_mock_ticker(pd.DataFrame())):
        with pytest.raises(AppError) as exc:
            YahooProvider().fetch_daily(get_instrument(KR_ID), START, END)
    assert exc.value.status_code == 503
    assert exc.value.code == "provider_unavailable"


def test_upstream_failure_hides_external_detail():
    boom = RuntimeError("yahoo 500 bad gateway secret-body")
    with patch("yfinance.Ticker", return_value=_mock_ticker(boom)):
        with pytest.raises(AppError) as exc:
            YahooProvider().fetch_daily(get_instrument(KR_ID), START, END)
    assert exc.value.status_code == 503
    assert exc.value.code == "provider_unavailable"
    assert "secret-body" not in str(exc.value.message)
    assert "bad gateway" not in str(exc.value.message)


def test_unknown_instrument_raises_404():
    with pytest.raises(AppError) as exc:
        get_instrument("NOPE")
    assert exc.value.status_code == 404
    assert exc.value.code == "instrument_not_found"
