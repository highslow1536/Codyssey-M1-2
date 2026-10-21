"""Yahoo intraday quote provider tests (injected frames only, no network)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.catalog import get_instrument
from app.errors import AppError
from app.providers.quotes import YahooQuoteProvider

UTC = timezone.utc
NY = "America/New_York"
AAPL = get_instrument("NASDAQ_AAPL")
SAMSUNG = get_instrument("KRX_005930")

_TICKER_SPEC = ["history", "history_metadata", "get_history_metadata"]


def _frame(times, closes, volumes=None, dividends=None, splits=None, tz=NY):
    n = len(times)
    idx = pd.DatetimeIndex([pd.Timestamp(t).tz_convert(tz) for t in times])
    closes = list(closes)
    frame = pd.DataFrame(
        {
            "Open": [c * 0.99 for c in closes],
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.98 for c in closes],
            "Close": closes,
            "Adj Close": closes,
            "Volume": [1000] * n if volumes is None else list(volumes),
            "Dividends": [0.0] * n if dividends is None else list(dividends),
            "Stock Splits": [0.0] * n if splits is None else list(splits),
        },
        index=idx,
    )
    return frame


def _ny(h, minute, day="2024-01-08"):
    return datetime.fromisoformat(f"{day}T{h:02d}:{minute:02d}:00+00:00").astimezone(ZoneInfo(NY))


GOOD_AAPL_META = {"symbol": "AAPL", "exchangeTimezoneName": NY, "currency": "USD"}
GOOD_KRX_META = {"symbol": "005930.KS", "exchangeTimezoneName": "Asia/Seoul", "currency": "KRW"}


def _provider(frame, metadata=None, seen=None):
    def history_fn(symbol):
        if seen is not None:
            seen.append(symbol)
        if metadata is None:
            return frame, dict(GOOD_AAPL_META)
        return frame, dict(metadata)

    return YahooQuoteProvider(history_fn=history_fn)


def test_complete_bars_only_and_event_times():
    frame = _frame(
        [_ny(14, 30), _ny(14, 35), _ny(14, 40), _ny(14, 45)],
        [100.0, 101.0, 102.0, 103.0],
    )
    now = _ny(14, 47).astimezone(UTC)
    quotes = _provider(frame).fetch(AAPL, now)
    assert [q["price_decimal"] for q in quotes] == ["100.0", "101.0", "102.0"]
    first = quotes[0]
    assert first["instrument_id"] == "NASDAQ_AAPL"
    assert first["resolution"] == "5m"
    assert first["session"] == "regular"
    assert first["event_time"] - first["bar_start"] == pd.Timedelta(minutes=5)
    assert first["event_time"].tzinfo is not None
    assert first["session_date"] == "2024-01-08"
    assert first["provider"] == "yahoo"
    assert "AAPL" in first["source_url"]
    assert first["currency"] == "USD"
    assert first["timezone"] == NY
    assert first["delay_seconds"] == 0
    assert first["quality"] == "ok"
    assert first["fetched_at"] == now
    assert first["gap"] is False
    assert first["corporate_action"] is False


def test_future_bars_rejected():
    frame = _frame([_ny(14, 30), _ny(15, 30)], [100.0, 101.0])
    quotes = _provider(frame).fetch(AAPL, _ny(14, 47).astimezone(UTC))
    assert [q["price_decimal"] for q in quotes] == ["100.0"]


def test_weekend_and_outside_session_excluded():
    saturday = datetime(2024, 1, 13, 14, 35, tzinfo=UTC)
    frame = _frame([_ny(13, 0), saturday], [100.0, 101.0])  # 08:00 ET + Saturday
    quotes = _provider(frame).fetch(AAPL, datetime(2024, 1, 16, 21, 5, tzinfo=UTC))
    assert quotes == []


def test_early_close_bars_after_close_excluded():
    # 2023-11-24: US early close at 13:00 ET (helper takes UTC: 17:50 UTC == 12:50 ET).
    frame = _frame(
        [_ny(17, 50, "2023-11-24"), _ny(17, 55, "2023-11-24"), _ny(18, 0, "2023-11-24")],
        [100.0, 101.0, 102.0],
    )
    quotes = _provider(frame).fetch(AAPL, _ny(19, 0, "2023-11-24").astimezone(UTC))
    assert [q["price_decimal"] for q in quotes] == ["100.0", "101.0"]


def test_price_keeps_full_precision():
    frame = _frame([_ny(14, 30)], [261.8028869628906])
    (quote,) = _provider(frame).fetch(AAPL, _ny(14, 40).astimezone(UTC))
    assert quote["price_decimal"] == "261.8028869628906"
    assert Decimal(quote["price_decimal"]) == Decimal("261.8028869628906")


def test_invalid_closes_dropped_without_fabrication():
    frame = _frame(
        [_ny(14, 30), _ny(14, 35), _ny(14, 40), _ny(14, 45)],
        [100.0, float("nan"), -5.0, 103.0],
    )
    quotes = _provider(frame).fetch(AAPL, _ny(14, 55).astimezone(UTC))
    assert [q["price_decimal"] for q in quotes] == ["100.0", "103.0"]
    assert quotes[1]["gap"] is True  # two missing bars in the same session


def test_krx_delayed_quality_and_source():
    kst = ZoneInfo("Asia/Seoul")
    base = datetime(2024, 1, 8, 0, 0, tzinfo=UTC)  # 09:00 KST, XKRX open
    idx = pd.DatetimeIndex([base, base + pd.Timedelta(minutes=5)])
    frame = pd.DataFrame(
        {
            "Open": [70000.0, 70100.0],
            "High": [70200.0, 70300.0],
            "Low": [69900.0, 70000.0],
            "Close": [70100.0, 70200.0],
            "Adj Close": [70100.0, 70200.0],
            "Volume": [10, 20],
            "Dividends": [0.0, 0.0],
            "Stock Splits": [0.0, 0.0],
        },
        index=idx,
    )
    _ = kst
    quotes = _provider(frame, GOOD_KRX_META).fetch(SAMSUNG, base + pd.Timedelta(minutes=15))
    assert len(quotes) == 2
    assert quotes[0]["currency"] == "KRW"
    assert quotes[0]["quality"] == "delayed"
    assert quotes[0]["delay_seconds"] == 1200
    assert "005930.KS" in quotes[0]["source_url"]
    assert quotes[0]["session_date"] == "2024-01-08"
    assert quotes[0]["volume"] == 10


def test_corporate_action_marker():
    frame = _frame(
        [_ny(14, 30), _ny(14, 35)], [100.0, 101.0], dividends=[0.0, 2.5], splits=[0.0, 0.0]
    )
    quotes = _provider(frame).fetch(AAPL, _ny(14, 45).astimezone(UTC))
    assert quotes[0]["corporate_action"] is False
    assert quotes[1]["corporate_action"] is True
    assert quotes[1]["dividends"] == 2.5


def test_yfinance_called_with_intraday_kwargs():
    ticker = MagicMock(spec=_TICKER_SPEC)
    ticker.history_metadata = dict(GOOD_AAPL_META)
    ticker.get_history_metadata.return_value = dict(GOOD_AAPL_META)
    ticker.history.return_value = _frame([_ny(14, 30)], [100.0])
    with patch("yfinance.Ticker", return_value=ticker):
        YahooQuoteProvider().fetch(AAPL, _ny(14, 40).astimezone(UTC))
    _, kwargs = ticker.history.call_args
    assert kwargs["period"] == "5d"
    assert kwargs["interval"] == "5m"
    assert kwargs["auto_adjust"] is False
    assert kwargs["prepost"] is False
    assert kwargs["actions"] is True
    assert ticker.get_history_metadata.call_count <= 1


@pytest.mark.parametrize(
    "metadata",
    [
        {"symbol": "WRONG", "exchangeTimezoneName": NY, "currency": "USD"},
        {"symbol": "AAPL", "exchangeTimezoneName": "Asia/Seoul", "currency": "USD"},
        {"symbol": "AAPL", "exchangeTimezoneName": NY, "currency": "KRW"},
    ],
)
def test_metadata_mismatch_raises_503(metadata):
    frame = _frame([_ny(14, 30)], [100.0])
    with pytest.raises(AppError) as exc:
        _provider(frame, metadata).fetch(AAPL, _ny(14, 40).astimezone(UTC))
    assert exc.value.status_code == 503
    assert exc.value.code == "provider_unavailable"


def test_matching_metadata_passes():
    frame = _frame([_ny(14, 30)], [100.0])
    meta = {"symbol": "AAPL", "exchangeTimezoneName": NY, "currency": "USD"}
    quotes = _provider(frame, meta).fetch(AAPL, _ny(14, 40).astimezone(UTC))
    assert len(quotes) == 1


def test_missing_currency_metadata_fails_closed_for_cash():
    frame = _frame([_ny(14, 30)], [100.0])
    no_ccy = {"symbol": "AAPL", "exchangeTimezoneName": NY}
    with pytest.raises(AppError) as exc:
        _provider(frame, no_ccy).fetch(AAPL, _ny(14, 40).astimezone(UTC))
    assert exc.value.status_code == 503
    assert exc.value.code == "provider_unavailable"


def test_absent_metadata_fails_closed_for_cash():
    frame = _frame([_ny(14, 30)], [100.0])
    with pytest.raises(AppError) as exc:
        _provider(frame, {}).fetch(AAPL, _ny(14, 40).astimezone(UTC))
    assert exc.value.status_code == 503
    assert exc.value.code == "provider_unavailable"


def test_point_index_needs_no_currency_metadata():
    from app.catalog import get_instrument as _get

    index = _get("INDEX_SP500")
    frame = _frame([_ny(14, 30)], [5000.0])
    quotes = _provider(frame, {}).fetch(index, _ny(14, 40).astimezone(UTC))
    assert len(quotes) == 1
    assert quotes[0]["currency"] == "POINT"


def test_missing_close_column_raises_503():
    idx = pd.DatetimeIndex([_ny(14, 30)])
    frame = pd.DataFrame({"Open": [100.0], "Volume": [10]}, index=idx)
    with pytest.raises(AppError) as exc:
        _provider(frame).fetch(AAPL, _ny(14, 40).astimezone(UTC))
    assert exc.value.status_code == 503


def test_empty_frame_returns_empty():
    assert _provider(pd.DataFrame()).fetch(AAPL, _ny(14, 40).astimezone(UTC)) == []


def test_upstream_failure_hides_external_detail():
    def boom(symbol):
        raise RuntimeError("yahoo 500 secret-body")

    with pytest.raises(AppError) as exc:
        YahooQuoteProvider(history_fn=boom).fetch(AAPL, _ny(14, 40).astimezone(UTC))
    assert exc.value.status_code == 503
    assert "secret-body" not in exc.value.message


def test_invalid_inputs_raise_bounded_errors():
    frame = _frame([_ny(14, 30)], [100.0])
    with pytest.raises(AppError) as exc:
        _provider(frame).fetch(AAPL, _ny(14, 40).replace(tzinfo=None))
    assert exc.value.status_code == 422
    with pytest.raises(AppError) as exc:
        _provider(frame).fetch({"id": "X"}, _ny(14, 40).astimezone(UTC))
    assert exc.value.status_code == 422


def test_lazy_mapping_metadata_reads_identity_without_loading_trading_periods():
    from collections.abc import Mapping
    from types import SimpleNamespace

    class LazyMetadata(Mapping):
        def __getitem__(self, key):
            if key == "tradingPeriods":
                raise AssertionError("must not request unrelated lazy metadata")
            return {
                "symbol": "AAPL",
                "currency": "USD",
                "exchangeTimezoneName": "America/New_York",
            }[key]

        def __iter__(self):
            return iter(["symbol", "currency", "exchangeTimezoneName", "tradingPeriods"])

        def __len__(self):
            return 4

    values = YahooQuoteProvider._history_metadata_once(
        SimpleNamespace(history_metadata=LazyMetadata())
    )
    assert values["currency"] == "USD"
    assert values["symbol"] == "AAPL"
    assert "tradingPeriods" not in values
