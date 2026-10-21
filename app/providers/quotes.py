"""Yahoo Finance 5-minute intraday quote provider (real data only).

Contract (exact, for the Firestore/API/worker owner = main):

- ``YahooQuoteProvider(settings=None)`` with
  ``fetch(instrument, now) -> list[quote]``. ``instrument`` is a catalog
  dict (same required keys as :class:`app.providers.yahoo.YahooProvider`:
  ``id`` / ``yahoo_symbol`` / ``market`` / ``currency`` / ``timezone`` /
  ``calendar`` / ``delay_seconds``). ``now`` must be an aware datetime and
  is recorded as each quote's ``fetched_at``.
- Upstream call is exactly ``Ticker.history(period='5d', interval='5m',
  auto_adjust=False, prepost=False, actions=True)``. ``period='5d'`` is a
  chosen bounded window (Yahoo serves 5m bars up to 60d).
  ``prepost=False`` already excludes pre/post market, and the provider
  additionally keeps ONLY bars fully inside the exchange-calendar regular
  session (holidays, early closes and lunch breaks via ``exchange_calendars``
  schedule open/break/close columns). History metadata (symbol / exchange
  timezone / currency) is cross-checked with the same rules as
  ``YahooProvider``; any mismatch fails with ``provider_unavailable``.
  For non-POINT instruments the source currency must be present and match
  the catalog entry: absent metadata or a missing currency field fails closed with
  ``provider_unavailable`` instead of assuming the catalog value.
- Only COMPLETE bars are returned: ``bar_start + 5m <= now``. Bars ending
  after ``now`` (incomplete) and bars starting after ``now`` (future) are
  dropped. Fetched data is already exchange-delayed, so the provider does
  NOT subtract ``delay_seconds`` again; freshness (latest ``event_time``
  vs ``delay_seconds + 300 + poll + 120``), regular-session scheduling and
  replay of data gaps are owned by main.
- Price is the raw bar ``Close`` as a full-precision positive finite
  ``Decimal`` string (``format(Decimal(str(close)), 'f')``); it is NEVER
  rounded to alert precision so comparisons stay exact. Non-positive or
  non-finite closes are dropped without fabrication. There is no daily
  fallback and no synthetic bars.
- Quote keys: ``instrument_id``, ``resolution`` (``"5m"``), ``currency``,
  ``timezone``, ``price_decimal``, ``bar_start`` / ``event_time`` (= bar
  end; aware UTC datetimes), ``fetched_at``, ``session_date``
  (``YYYY-MM-DD`` in the instrument timezone), ``gap`` (True when the
  previous expected 5m bar of the same session is missing),
  ``provider`` (``"yahoo"``), ``source_url``, ``delay_seconds``,
  ``quality`` (``"delayed"`` when ``delay_seconds > 0`` else ``"ok"``),
  ``volume`` (int or None), ``dividends`` / ``stock_splits`` (floats) and
  ``corporate_action`` (bool; True when dividends > 0 or splits != 0, so
  main can pause the watch and ask for new thresholds). ``session`` is the
  literal ``"regular"``.
- Empty result (e.g. weekend with no complete regular bars in the window)
  returns ``[]``; it is NOT an error. Upstream failures, unusable frames
  and metadata mismatches raise bounded :class:`AppError` (``422``
  ``invalid_instrument`` / ``invalid_time``, ``503``
  ``provider_unavailable`` / ``calendar_unavailable``) whose messages never
  carry upstream text.
- Test seam: pass ``history_fn(symbol) -> (frame, metadata_dict)`` to the
  constructor to inject deterministic frames; the default path uses
  ``yfinance`` and reads ``history_metadata`` at most once.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable
from zoneinfo import ZoneInfo

from app.errors import AppError

BAR_MINUTES = 5
BAR_DELTA = timedelta(minutes=BAR_MINUTES)
FETCH_PERIOD = "5d"
FETCH_INTERVAL = "5m"
GAP_AFTER_SECONDS = 305
_META_KEY = "yahoo_intraday_metadata"
_REQUIRED_KEYS = (
    "id",
    "yahoo_symbol",
    "market",
    "currency",
    "timezone",
    "calendar",
    "delay_seconds",
)
_US_EASTERN_ALIASES = {"US/Eastern": "America/New_York"}

HistoryFn = Callable[[str], tuple]


class YahooQuoteProvider:
    """Fetch complete regular-session 5m bars via yfinance."""

    def __init__(self, settings=None, history_fn: HistoryFn | None = None) -> None:
        self._settings = settings
        self._history_fn = history_fn

    def fetch(self, instrument: dict, now: datetime) -> list[dict]:
        """Return sorted complete regular-session quotes at/before ``now``."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise AppError(422, "invalid_time", "now must be a timezone-aware datetime.")
        ref = now.astimezone(timezone.utc)
        symbol, inst_tz, calendar, delay = self._validated_instrument(instrument)
        instrument_id = str(instrument["id"])
        try:
            frame, metadata = self._download(symbol)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(503, "provider_unavailable", self._safe_message(instrument_id)) from exc
        try:
            return self._parse_frame(
                frame=frame,
                metadata=metadata,
                instrument=instrument,
                symbol=symbol,
                calendar=calendar,
                inst_tz=inst_tz,
                delay=delay,
                now=ref,
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(503, "provider_unavailable", self._safe_message(instrument_id)) from exc

    @staticmethod
    def _safe_message(instrument_id: str) -> str:
        return "Yahoo intraday data is temporarily unavailable for " + str(instrument_id) + "."

    @staticmethod
    def _validated_instrument(instrument: dict) -> tuple[str, str, str, int]:
        if not isinstance(instrument, dict):
            raise AppError(422, "invalid_instrument", "instrument must be a catalog dict.")
        for key in _REQUIRED_KEYS:
            if instrument.get(key) is None:
                raise AppError(422, "invalid_instrument", "instrument is missing: " + key)
        try:
            delay = int(instrument["delay_seconds"])
        except (TypeError, ValueError) as exc:
            raise AppError(422, "invalid_instrument", "delay_seconds must be an int.") from exc
        if delay < 0:
            raise AppError(422, "invalid_instrument", "delay_seconds must be >= 0.")
        zone_name = str(instrument["timezone"])
        try:
            ZoneInfo(zone_name)
        except Exception as exc:
            raise AppError(422, "invalid_instrument", "Unknown timezone: " + zone_name) from exc
        return (
            str(instrument["yahoo_symbol"]),
            zone_name,
            str(instrument["calendar"]),
            delay,
        )

    def _download(self, symbol: str):
        if self._history_fn is not None:
            produced = self._history_fn(symbol)
            if isinstance(produced, tuple) and len(produced) == 2:
                return produced[0], (produced[1] or {})
            return produced, {}
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        frame = ticker.history(
            period=FETCH_PERIOD,
            interval=FETCH_INTERVAL,
            auto_adjust=False,
            prepost=False,
            actions=True,
        )
        metadata = self._history_metadata_once(ticker)
        try:
            if frame is not None and hasattr(frame, "attrs"):
                frame.attrs[_META_KEY] = metadata
        except Exception:
            pass
        return frame, metadata

    @staticmethod
    def _history_metadata_once(ticker) -> dict:
        from collections.abc import Mapping

        def required_fields(meta):
            if not isinstance(meta, Mapping):
                return {}
            # yfinance 1.7 returns a lazy Mapping. Reading only validation
            # fields avoids expanding unrelated lazy trading-period requests.
            keys = (
                "symbol",
                "ticker",
                "currency",
                "priceCurrency",
                "exchangeTimezoneName",
                "timeZoneFullName",
                "timezone",
            )
            return {key: meta.get(key) for key in keys if meta.get(key) is not None}

        try:
            meta = ticker.history_metadata
            values = required_fields(meta)
            if values:
                return values
        except Exception:
            pass
        try:
            getter = ticker.get_history_metadata
        except Exception:
            return {}
        try:
            meta = getter() if callable(getter) else getter
        except Exception:
            return {}
        return required_fields(meta)

    def _parse_frame(
        self, *, frame, metadata, instrument, symbol, calendar, inst_tz, delay, now
    ) -> list[dict]:
        import pandas as pd

        instrument_id = str(instrument["id"])
        if frame is None or getattr(frame, "empty", True):
            return []
        try:
            attrs = getattr(frame, "attrs", {}) or {}
            attached = attrs.get(_META_KEY, {})
        except Exception:
            attached = {}
        meta = metadata if isinstance(metadata, dict) and metadata else attached
        if not isinstance(meta, dict):
            meta = {}
        self._validate_history_metadata(meta, instrument, symbol, inst_tz)

        df = frame.copy()
        cols = getattr(df, "columns", None)
        if cols is not None and isinstance(cols, pd.MultiIndex):
            df.columns = cols.get_level_values(0)
        norm: dict[str, str] = {}
        for col in list(df.columns):
            norm[str(col).strip().lower().replace(" ", "_")] = str(col)

        def pick(*names: str):
            for name in names:
                if name in norm:
                    return norm[name]
            return None

        c_close = pick("close")
        if c_close is None:
            raise AppError(503, "provider_unavailable", self._safe_message(instrument_id))
        c_vol = pick("volume")
        c_div = pick("dividends", "dividend")
        c_split = pick("stock_splits", "stock-split", "splits", "split")

        zone = ZoneInfo(inst_tz)
        schedule = self._schedule_for(calendar, df.index, zone, instrument_id)
        currency = str(instrument["currency"])
        source_url = "https://finance.yahoo.com/quote/" + symbol
        quality = "delayed" if delay > 0 else "ok"

        rows: list[dict] = []
        for idx, row in df.iterrows():
            bar_start = self._bar_start_utc(idx, zone)
            if bar_start is None:
                continue
            if bar_start > now:
                continue  # future bar: reject
            bar_end = bar_start + BAR_DELTA
            if bar_end > now:
                continue  # incomplete bar: reject
            session_day = bar_start.astimezone(zone).date()
            if not self._is_regular_bar(schedule, session_day, bar_start, bar_end):
                continue
            try:
                price = self._req_positive_decimal(row.get(c_close))
            except (TypeError, ValueError, InvalidOperation):
                continue
            volume = self._opt_volume(row.get(c_vol) if c_vol is not None else None)
            if volume is False:
                continue
            dividends = self._opt_nonneg_float(
                row.get(c_div) if c_div is not None else 0.0, default=0.0
            )
            splits = self._opt_nonneg_float(
                row.get(c_split) if c_split is not None else 0.0, default=0.0
            )
            if dividends is None or splits is None:
                continue
            rows.append(
                {
                    "bar_start": bar_start,
                    "bar_end": bar_end,
                    "session_date": session_day.isoformat(),
                    "price_decimal": format(price, "f"),
                    "volume": volume,
                    "dividends": dividends,
                    "stock_splits": splits,
                }
            )
        rows.sort(key=lambda item: item["bar_start"])
        quotes: list[dict] = []
        previous: dict | None = None
        for item in rows:
            gap = False
            if previous is not None and item["session_date"] == previous["session_date"]:
                delta = (item["bar_start"] - previous["bar_start"]).total_seconds()
                gap = delta > GAP_AFTER_SECONDS
            corporate_action = bool(item["dividends"] > 0 or item["stock_splits"] != 0)
            quotes.append(
                {
                    "instrument_id": instrument_id,
                    "resolution": "5m",
                    "currency": currency,
                    "timezone": inst_tz,
                    "price_decimal": item["price_decimal"],
                    "bar_start": item["bar_start"],
                    "event_time": item["bar_end"],
                    "fetched_at": now,
                    "session_date": item["session_date"],
                    "gap": gap,
                    "provider": "yahoo",
                    "source_url": source_url,
                    "session": "regular",
                    "delay_seconds": delay,
                    "quality": quality,
                    "volume": item["volume"],
                    "dividends": item["dividends"],
                    "stock_splits": item["stock_splits"],
                    "corporate_action": corporate_action,
                }
            )
            previous = item
        return quotes

    def _schedule_for(self, calendar: str, index, zone: ZoneInfo, instrument_id: str):
        import pandas as pd

        try:
            import exchange_calendars as xcals
        except ImportError as exc:
            raise AppError(
                503, "calendar_unavailable", "Trading calendar is temporarily unavailable."
            ) from exc
        try:
            cal = xcals.get_calendar(calendar)
        except Exception as exc:
            raise AppError(
                503, "calendar_unavailable", "Trading calendar is temporarily unavailable."
            ) from exc
        try:
            stamps = pd.DatetimeIndex(list(index))
        except Exception as exc:
            raise AppError(503, "provider_unavailable", self._safe_message(instrument_id)) from exc
        days: list = []
        for stamp in stamps:
            try:
                ts = pd.Timestamp(stamp)
                day = (
                    ts.tz_convert(str(zone)).date()
                    if ts.tzinfo is not None
                    else ts.tz_localize(str(zone)).date()
                )
                days.append(day)
            except Exception:
                continue
        if not days:
            return None
        start = min(days) - timedelta(days=1)
        end = max(days) + timedelta(days=1)
        try:
            return cal.schedule.loc[str(start) : str(end)]
        except Exception as exc:
            raise AppError(
                503, "calendar_unavailable", "Trading calendar is temporarily unavailable."
            ) from exc

    @staticmethod
    def _is_regular_bar(schedule, session_day, bar_start: datetime, bar_end: datetime) -> bool:
        import pandas as pd

        if schedule is None or getattr(schedule, "empty", True):
            return False
        try:
            row = schedule.loc[str(session_day)]
        except KeyError:
            return False  # holiday / weekend: no session
        if hasattr(row, "empty") and row.empty:
            return False
        try:
            open_utc = pd.Timestamp(row["open"]).tz_convert("UTC")
            close_utc = pd.Timestamp(row["close"]).tz_convert("UTC")
        except Exception:
            return False
        start_ts = pd.Timestamp(bar_start).tz_convert("UTC")
        end_ts = pd.Timestamp(bar_end).tz_convert("UTC")
        if start_ts < open_utc or end_ts > close_utc:
            return False
        try:
            break_start = pd.Timestamp(row["break_start"])
            break_end = pd.Timestamp(row["break_end"])
            if not (pd.isna(break_start) or pd.isna(break_end)):
                brk_start = break_start.tz_convert("UTC")
                brk_end = break_end.tz_convert("UTC")
                if not (end_ts <= brk_start or start_ts >= brk_end):
                    return False
        except (KeyError, TypeError, ValueError):
            pass
        return True

    def _validate_history_metadata(
        self, meta: dict, instrument: dict, symbol: str, inst_tz: str
    ) -> None:
        instrument_id = str(instrument.get("id", "?"))

        def mismatch() -> AppError:
            return AppError(503, "provider_unavailable", self._safe_message(instrument_id))

        if not isinstance(meta, dict) or not meta:
            # Fail closed: without source metadata there is no evidence the
            # feed currency matches the catalog (POINT needs no currency).
            if str(instrument.get("currency", "POINT")).upper() != "POINT":
                raise mismatch()
            return

        raw_symbol = meta.get("symbol", meta.get("ticker"))
        if isinstance(raw_symbol, str) and raw_symbol and raw_symbol.upper() != symbol.upper():
            raise mismatch()
        raw_tz = meta.get("exchangeTimezoneName") or meta.get("timeZoneFullName")
        if not raw_tz and "/" in str(meta.get("timezone", "")):
            raw_tz = meta["timezone"]
        if raw_tz and _US_EASTERN_ALIASES.get(raw_tz, raw_tz) != inst_tz:
            raise mismatch()
        if str(instrument.get("currency", "POINT")).upper() != "POINT":
            raw_ccy = meta.get("currency", meta.get("priceCurrency"))
            if (
                not isinstance(raw_ccy, str)
                or not raw_ccy
                or raw_ccy.upper() != str(instrument["currency"]).upper()
            ):
                raise mismatch()

    @staticmethod
    def _bar_start_utc(idx, zone: ZoneInfo):
        import pandas as pd

        try:
            ts = pd.Timestamp(idx)
            if ts.tzinfo is None:
                ts = ts.tz_localize(str(zone))
            return ts.tz_convert("UTC").to_pydatetime()
        except Exception:
            return None

    @staticmethod
    def _req_positive_decimal(value) -> Decimal:
        import pandas as pd

        if value is None:
            raise ValueError("missing")
        try:
            if pd.isna(value):
                raise ValueError("missing")
        except TypeError:
            pass
        num = Decimal(str(value))
        if not num.is_finite() or num <= 0:
            raise ValueError("non-positive")
        return num

    @staticmethod
    def _opt_volume(value):
        import pandas as pd

        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except TypeError:
            pass
        num = float(value)
        if not math.isfinite(num) or num < 0:
            return False
        return int(num)

    @staticmethod
    def _opt_nonneg_float(value, *, default: float):
        import pandas as pd

        if value is None:
            return default
        try:
            if pd.isna(value):
                return default
        except TypeError:
            pass
        num = float(value)
        if not math.isfinite(num) or num < 0:
            return None
        return num
