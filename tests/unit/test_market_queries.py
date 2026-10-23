"""Unit tests for the read-only MCP market query service.

No network and no Firestore emulator: providers, clock and repository
surfaces are stubbed. The stubs intentionally lack AlertRepository.apply /
queue and MarketRepository.save_bars, so any alert evaluation or owner-data
write would fail loudly instead of passing silently.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.catalog import get_instrument
from app.domain.indicators import calculate_indicators
from app.errors import AppError
from app.services.market_queries import MAX_BARS, MarketQueryService

UTC = timezone.utc
SP = get_instrument("INDEX_SP500")

# Monday 2024-01-08 14:35 ET: XNYS regular session is open.
MON_OPEN = datetime(2024, 1, 8, 19, 35, tzinfo=UTC)
# Saturday: no regular session for either KR or US calendars.
SATURDAY = datetime(2024, 1, 13, 15, 0, tzinfo=UTC)


class FakeSettings:
    data_poll_minutes = 5


class StubAlerts:
    """Minimal price_streams/price_quotes surface; no apply/queue/status."""

    def __init__(self, *, token="tok", latest=None, cached=()):
        self.token = token
        self.latest = latest
        self.cached = list(cached)
        self.claims = []
        self.finished = []

    def claim_poll(self, instrument_id, now, poll_minutes):
        self.claims.append((instrument_id, now, poll_minutes))
        return self.token

    def finish_poll(self, instrument_id, token, now, quotes=None, error=None):
        self.finished.append({"instrument_id": instrument_id, "quotes": quotes, "error": error})
        return True

    def latest_quote(self, instrument_id):
        return self.latest

    def cached_quotes(self, instrument_id, now, since=None):
        return list(self.cached)


class StubMarket:
    """Only the public-originals surface; notably no save_bars."""

    def __init__(self, history_bars=()):
        self.history_bars = list(history_bars)
        self.saved = []
        self.history_calls = []

    def save_original_bars(self, instrument, bars):
        self.saved.append((instrument["id"], list(bars)))

    def history(self, instrument_id, start_date, end_date):
        self.history_calls.append((instrument_id, start_date, end_date))
        return list(self.history_bars)


class FakeQuoteProvider:
    def __init__(self, quotes=(), error=None):
        self.quotes = list(quotes)
        self.error = error
        self.calls = []

    def fetch(self, instrument, now):
        self.calls.append((instrument["id"], now))
        if self.error is not None:
            raise self.error
        return list(self.quotes)


class FakeDailyProvider:
    def __init__(self, bars=(), error=None):
        self.bars = list(bars)
        self.error = error
        self.calls = []

    def fetch_daily(self, instrument, start, end):
        self.calls.append((instrument["id"], start, end))
        if self.error is not None:
            raise self.error
        return list(self.bars)


def _quote(price, event_time):
    return {
        "instrument_id": "NASDAQ_AAPL",
        "resolution": "5m",
        "session": "regular",
        "currency": "USD",
        "timezone": "America/New_York",
        "price_decimal": str(price),
        "bar_start": event_time - timedelta(minutes=5),
        "event_time": event_time,
        "fetched_at": event_time,
        "session_date": "2024-01-08",
        "gap": False,
        "provider": "yahoo",
        "source_url": "https://finance.yahoo.com/quote/AAPL",
        "delay_seconds": 0,
        "quality": "ok",
        "volume": 1000,
        "dividends": 0.0,
        "stock_splits": 0.0,
        "corporate_action": False,
    }


def _daily_bar(day, close, instrument_id="INDEX_SP500", volume=1000):
    return {
        "instrument_id": instrument_id,
        "resolution": "1d",
        "date": day,
        "event_time": datetime.fromisoformat(day + "T21:00:00+00:00"),
        "fetched_at": datetime(2025, 6, 2, tzinfo=UTC),
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "adjusted_close": close,
        "volume": volume,
        "dividends": 0.0,
        "stock_splits": 0.0,
        "provider": "yahoo",
        "source_url": "https://finance.yahoo.com/quote/%5EGSPC",
        "currency": "POINT",
        "timezone": "America/New_York",
        "delay_seconds": 0,
        "quality_status": "ok",
    }


def _service(
    *,
    now,
    quotes=(),
    quote_error=None,
    daily=(),
    daily_error=None,
    token="tok",
    latest=None,
    cached=(),
    history_cache=(),
):
    svc = MarketQueryService(
        MagicMock(),
        settings=FakeSettings(),
        quote_provider=FakeQuoteProvider(quotes, quote_error),
        daily_provider=FakeDailyProvider(daily, daily_error),
        clock=lambda: now,
    )
    svc.alerts = StubAlerts(token=token, latest=latest, cached=cached)
    svc.market = StubMarket(history_cache)
    return svc


def _days(start_day, count):
    base = date.fromisoformat(start_day)
    return [(base + timedelta(days=i)).isoformat() for i in range(count)]


def test_quote_fresh_delayed_ok_and_reuses_lease():
    fresh = _quote("231.5", MON_OPEN - timedelta(seconds=120))
    svc = _service(now=MON_OPEN, quotes=[fresh], latest=fresh)
    out = svc.quote("NASDAQ_AAPL")
    assert out["quality_status"] == "ok"
    assert out["market_open"] is True
    assert out["quote"]["price_decimal"] == "231.5"
    assert out["delay_seconds"] == 0
    assert out["age_seconds"] == pytest.approx(120.0)
    assert "지연" in out["observation_note"]
    assert "realtime" not in out["observation_note"].lower()
    assert out["error"] is None
    # Lease gated exactly once and the fetched bars were handed to the cache.
    assert len(svc.alerts.claims) == 1
    assert svc.alerts.finished[0]["quotes"] == [fresh]
    # Read-only: the stub exposes no alert evaluation surface at all.
    assert not hasattr(svc.alerts, "apply")
    assert not hasattr(svc.alerts, "queue")


def test_quote_falls_back_to_cached_quotes_when_stream_doc_missing():
    fresh = _quote("232.0", MON_OPEN - timedelta(seconds=60))
    svc = _service(now=MON_OPEN, quotes=[fresh], latest=None, cached=[fresh])
    out = svc.quote("NASDAQ_AAPL")
    assert out["quality_status"] == "ok"
    assert out["quote"]["price_decimal"] == "232.0"


def test_quote_without_observation_reports_unavailable_even_when_closed():
    svc = _service(now=SATURDAY, token=None, latest=None, cached=())
    out = svc.quote("NASDAQ_AAPL")
    assert out["quality_status"] == "unavailable"
    assert out["market_open"] is False
    assert out["quote"] is None
    assert svc.quote_provider.calls == []


def test_quote_stale_when_cache_is_old_during_session():
    old = _quote("230.0", MON_OPEN - timedelta(hours=3))
    svc = _service(now=MON_OPEN, token=None, latest=old)
    out = svc.quote("NASDAQ_AAPL")
    assert out["quality_status"] == "stale"
    assert out["market_open"] is True
    assert out["age_seconds"] > out["max_age_seconds"]
    assert svc.quote_provider.calls == []


def test_quote_provider_failure_without_cache_is_unavailable():
    failure = AppError(503, "quotes_unavailable", "boom")
    svc = _service(now=MON_OPEN, quote_error=failure, latest=None, cached=())
    out = svc.quote("NASDAQ_AAPL")
    assert out["quality_status"] == "unavailable"
    assert out["quote"] is None
    assert out["error"]["code"] == "quotes_unavailable"
    assert svc.alerts.finished[0]["error"]["code"] == "quotes_unavailable"


def test_history_fetches_and_caches_public_originals_only():
    bars = [_daily_bar(day, 100.0 + i) for i, day in enumerate(_days("2025-04-01", 5))]
    svc = _service(now=MON_OPEN, daily=bars)
    out = svc.history("INDEX_SP500")
    assert out["quality_status"] == "ok"
    assert out["observation_count"] == 5
    assert out["bars"] == bars
    assert out["price_basis"] == "adjusted_close"
    assert out["provider"] == "yahoo"
    assert out["error"] is None
    # Canonical originals are saved; the owner-scoped save_bars (editable
    # ``data`` side effects) does not even exist on the stub.
    assert svc.market.saved == [("INDEX_SP500", bars)]
    assert not hasattr(svc.market, "save_bars")


def test_history_is_bounded_to_recent_bars():
    bars = [_daily_bar(day, 100.0 + (i % 50)) for i, day in enumerate(_days("2020-01-01", 1100))]
    svc = _service(now=MON_OPEN, daily=bars)
    out = svc.history("INDEX_SP500")
    assert out["observation_count"] == MAX_BARS
    assert len(out["bars"]) == MAX_BARS
    assert out["bars"][0] == bars[-MAX_BARS]
    assert out["bars"][-1] == bars[-1]


def test_history_rejects_bad_date_ranges():
    svc = _service(now=MON_OPEN, daily=[])
    with pytest.raises(AppError):
        svc.history("INDEX_SP500", date(2025, 2, 1), date(2025, 1, 1))
    with pytest.raises(AppError):
        svc.history("INDEX_SP500", date(2022, 1, 1), date(2025, 1, 1))


def test_history_provider_failure_with_empty_cache_is_unavailable():
    failure = AppError(503, "provider_unavailable", "boom")
    svc = _service(now=MON_OPEN, daily_error=failure, history_cache=[])
    out = svc.history("INDEX_SP500")
    assert out["quality_status"] == "unavailable"
    assert out["bars"] == []
    assert out["observation_count"] == 0
    assert out["error"]["code"] == "provider_unavailable"


def test_indicators_match_shared_calculation_over_full_history():
    days = _days("2025-04-01", 30)
    bars = [_daily_bar(day, 100.0 + i) for i, day in enumerate(days)]
    svc = _service(now=MON_OPEN, daily=bars)
    out = svc.indicators("INDEX_SP500", date(2025, 4, 1), date(2025, 4, 30))
    expected = calculate_indicators(bars, SP)
    for key in (
        "observation_count",
        "latest_close",
        "sma_20",
        "rsi_14",
        "returns_pct",
        "trend",
        "realized_volatility_20d_pct",
        "volume_ratio_20d",
    ):
        assert out[key] == expected[key]
    assert out["provider"] == "yahoo"
    assert out["error"] is None
    assert svc.market.saved == [("INDEX_SP500", bars)]


def test_indicators_fall_back_to_canonical_cache_as_stale():
    days = _days("2025-04-01", 25)
    cached = [_daily_bar(day, 100.0 + i) for i, day in enumerate(days)]
    failure = AppError(503, "provider_unavailable", "boom")
    svc = _service(now=MON_OPEN, daily_error=failure, history_cache=cached)
    out = svc.indicators("INDEX_SP500", date(2025, 4, 1), date(2025, 4, 25))
    assert out["observation_count"] == 25
    assert out["quality_status"] == "stale"
    assert out["error"]["code"] == "provider_unavailable"
    assert out["latest_close"] == 124.0
    assert svc.market.saved == []
    assert svc.daily_provider.calls != []


def test_cached_daily_observation_time_is_not_replaced_by_query_time():
    bars = [_daily_bar(day, 100.0 + i) for i, day in enumerate(_days("2025-04-01", 25))]
    query_time = datetime(2026, 10, 23, tzinfo=UTC)
    svc = _service(
        now=query_time,
        daily_error=AppError(503, "provider_unavailable", "failed"),
        history_cache=bars,
    )
    for out in (svc.history("INDEX_SP500"), svc.indicators("INDEX_SP500")):
        assert out["quality_status"] == "stale"
        assert out["fetched_at"] == bars[-1]["fetched_at"]
        assert out["queried_at"] == query_time


def test_future_quote_or_unknown_calendar_is_never_current(monkeypatch):
    quote = _quote("230", MON_OPEN + timedelta(minutes=5))
    svc = _service(now=MON_OPEN, token=None, latest=quote)
    assert svc.quote("NASDAQ_AAPL")["quality_status"] == "stale"
    monkeypatch.setattr(
        "app.services.market_queries.market_is_open",
        lambda *args: (_ for _ in ()).throw(AppError(503, "calendar_unavailable", "unavailable")),
    )
    svc.alerts.latest = _quote("230", MON_OPEN - timedelta(seconds=30))
    out = svc.quote("NASDAQ_AAPL")
    assert out["market_open"] is None
    assert out["quality_status"] == "stale"
