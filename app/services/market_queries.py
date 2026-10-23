"""Read-only market queries for the MCP adapter.

The service shares the alert worker's intraday cache (``price_streams`` /
``price_quotes`` through :class:`AlertRepository`) without evaluating or
sending alerts, fetches daily bars through the existing ``YahooProvider``,
persists them as public originals through
``MarketRepository.save_original_bars`` (never the owner-scoped
``save_bars``, which would create editable ``data`` documents), and computes
indicators with ``calculate_indicators`` (no duplicated formulas).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.catalog import get_instrument
from app.config import get_settings
from app.domain.indicators import calculate_indicators
from app.domain.sessions import _require_calendar, complete_sessions
from app.errors import AppError
from app.providers.quotes import YahooQuoteProvider
from app.providers.yahoo import YahooProvider
from app.repositories.alerts import AlertRepository
from app.repositories.market import MarketRepository
from app.services.data import date_range

MAX_BARS = 1000


def market_is_open(instrument, now):
    try:
        return _require_calendar(instrument["calendar"]).is_open_on_minute(
            pd.Timestamp(now).floor("min")
        )
    except Exception as exc:
        raise AppError(503, "calendar_unavailable", "거래소 일정을 확인하지 못했습니다.") from exc


def _as_utc(value):
    """Coerce a stored timestamp to aware UTC; None when unparseable."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


class MarketQueryService:
    """Read-only quotes, history and indicators for MCP tool use.

    ``quote_provider`` / ``daily_provider`` / ``clock`` are injectable seams
    for tests; production callers leave them unset.
    """

    def __init__(self, db, settings=None, *, quote_provider=None, daily_provider=None, clock=None):
        self.db = db
        self.settings = settings or get_settings()
        self.alerts = AlertRepository(db)
        self.market = MarketRepository(db)
        self.quote_provider = quote_provider or YahooQuoteProvider(self.settings)
        self.daily_provider = daily_provider or YahooProvider()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def quote(self, instrument_id):
        """Return the latest cached 5-minute quote with explicit freshness.

        Refresh is gated by the shared poll lease (``claim_poll`` /
        ``finish_poll``) so MCP reads reuse the worker cache instead of
        hammering Yahoo. Quotes are exchange-delayed observations, never
        realtime: ``quality_status`` is ``ok`` (fresh delayed), ``stale``,
        ``market_closed`` or ``unavailable``.
        """
        instrument = get_instrument(instrument_id)
        now = self.clock()
        poll_minutes = self.settings.data_poll_minutes
        error = None
        try:
            market_open = market_is_open(instrument, now)
        except AppError as exc:
            market_open = None
            error = {"code": exc.code, "message": exc.message}
        token = self.alerts.claim_poll(instrument_id, now, poll_minutes)
        if token:
            try:
                quotes = self.quote_provider.fetch(instrument, now)
            except AppError as exc:
                self.alerts.finish_poll(
                    instrument_id,
                    token,
                    self.clock(),
                    error={"code": exc.code, "message": exc.message},
                )
                if error is None:
                    error = {"code": exc.code, "message": exc.message}
            except Exception:
                self.alerts.finish_poll(
                    instrument_id,
                    token,
                    self.clock(),
                    error={
                        "code": "quotes_unavailable",
                        "message": "Yahoo 장중 시세를 수집하지 못했습니다.",
                    },
                )
                if error is None:
                    error = {
                        "code": "quotes_unavailable",
                        "message": "Yahoo 장중 시세를 수집하지 못했습니다.",
                    }
            else:
                self.alerts.finish_poll(instrument_id, token, self.clock(), quotes=quotes)
        latest = self.alerts.latest_quote(instrument_id)
        if latest is None:
            cached = self.alerts.cached_quotes(instrument_id, now) or []
            latest = cached[-1] if cached else None
        delay = int(instrument["delay_seconds"])
        max_age = delay + 300 + poll_minutes * 60 + 120
        note = (
            "Yahoo 정규장 5분 봉 종가의 지연 관측입니다 (안내 지연 약 "
            + f"{delay // 60}분). 실시간 시세가 아니며, 관측 시각(event_time)과 "
            + "조회 시각(queried_at)을 함께 확인하세요."
        )
        envelope = {
            "instrument_id": instrument_id,
            "provider": "yahoo",
            "market_open": market_open,
            "max_age_seconds": max_age,
            "delay_seconds": delay,
            "source_url": ("https://finance.yahoo.com/quote/" + str(instrument["yahoo_symbol"])),
            "observation_note": note,
            "queried_at": now,
            "error": error,
        }
        if latest is None:
            return {**envelope, "quality_status": "unavailable", "quote": None, "age_seconds": None}
        event_time = _as_utc(latest.get("event_time"))
        age = (now - event_time).total_seconds() if event_time else None
        if market_open is False:
            quality = "market_closed"
        elif market_open is None or age is None or age < 0 or age > max_age:
            quality = "stale"
        else:
            quality = "ok"
        return {**envelope, "quality_status": quality, "quote": dict(latest), "age_seconds": age}

    @staticmethod
    def _bounded(bars):
        """Keep at most the most recent MAX_BARS daily bars."""
        return list(bars[-MAX_BARS:])

    def _fallback_bars(self, instrument_id, start, end):
        """Canonical cache read used only when Yahoo daily fetch fails."""
        try:
            return self.market.history(instrument_id, start, end)
        except Exception:
            return []

    def history(self, instrument_id, start_date=None, end_date=None):
        """Fetch actual Yahoo daily bars and cache them as public originals.

        Uses ``save_original_bars`` so no owner ``data`` document is created
        or modified. On provider failure, falls back to the canonical cache
        (``stale``) or reports ``unavailable``; only invalid instruments and
        date ranges raise.
        """
        instrument = get_instrument(instrument_id)
        start, end = date_range(start_date, end_date)
        now = self.clock()
        try:
            bars = self.daily_provider.fetch_daily(instrument, start, end)
        except AppError as exc:
            cached = self._fallback_bars(instrument_id, start, end)
            return self._history_payload(
                instrument,
                start,
                end,
                cached,
                now,
                error={"code": exc.code, "message": exc.message},
                fallback=True,
            )
        except Exception:
            cached = self._fallback_bars(instrument_id, start, end)
            return self._history_payload(
                instrument,
                start,
                end,
                cached,
                now,
                error={
                    "code": "provider_unavailable",
                    "message": (
                        "Yahoo data is temporarily unavailable for " + str(instrument_id) + "."
                    ),
                },
                fallback=True,
            )
        self.market.save_original_bars(instrument, bars)
        return self._history_payload(instrument, start, end, bars, now)

    def _history_payload(self, instrument, start, end, bars, now, *, error=None, fallback=False):
        kept = self._bounded(list(bars or []))
        if kept:
            quality = "stale" if fallback else "ok"
            start_value = kept[0].get("date", start.isoformat())
            end_value = kept[-1].get("date", end.isoformat())
        else:
            quality = "unavailable"
            start_value = start.isoformat()
            end_value = end.isoformat()
        sources = sorted({str(bar["source_url"]) for bar in kept if bar.get("source_url")})
        return {
            "instrument_id": instrument["id"],
            "provider": "yahoo",
            "price_basis": "adjusted_close",
            "requested_start_date": start.isoformat(),
            "requested_end_date": end.isoformat(),
            "start_date": start_value,
            "end_date": end_value,
            "observation_count": len(kept),
            "bars": kept,
            "quality_status": quality,
            "source_urls": sources,
            "fetched_at": max(
                (stamp for bar in kept if (stamp := _as_utc(bar.get("fetched_at")))),
                default=None,
            ),
            "queried_at": now,
            "error": error,
        }

    def indicators(self, instrument_id, start_date=None, end_date=None):
        """Calculate indicators over the full requested daily history.

        Same fetch-and-cache path as :meth:`history`, then the shared
        ``calculate_indicators`` over user-editable data-free canonical bars.
        """
        instrument = get_instrument(instrument_id)
        start, end = date_range(start_date, end_date)
        history = self.history(instrument_id, start, end)
        now = history["queried_at"]
        kept = history["bars"]
        if not kept:
            return {key: value for key, value in history.items() if key != "bars"}
        result = calculate_indicators(kept, instrument)
        try:
            sessions = complete_sessions(
                instrument["calendar"], instrument["timezone"], start, end, now
            )
        except AppError:
            sessions = []
        expected = sessions[-1].isoformat() if sessions else None
        result["expected_latest_session"] = expected
        if expected and result.get("end_date") != expected and result.get("quality_status") == "ok":
            result["quality_status"] = "stale"
        if history["quality_status"] == "stale" and result.get("quality_status") == "ok":
            result["quality_status"] = "stale"
        result.setdefault("instrument_id", instrument["id"])
        result["requested_start_date"] = start.isoformat()
        result["requested_end_date"] = end.isoformat()
        result["fetched_at"] = history["fetched_at"]
        result["queried_at"] = now
        result["error"] = history["error"]
        return result
