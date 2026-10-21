"""Yahoo Finance daily-bar provider (bounded slice, no fake data)."""

from __future__ import annotations

import inspect
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.domain.sessions import complete_sessions, session_close_utc
from app.errors import AppError

_REQUIRED_KEYS = (
    "id",
    "yahoo_symbol",
    "market",
    "currency",
    "timezone",
    "calendar",
    "delay_seconds",
)
_META_KEY = "yahoo_metadata"


class YahooProvider:
    """Fetch complete daily bars via yfinance; constructor accepts no args."""

    def __init__(self, settings=None) -> None:
        self._settings = settings

    def fetch_daily(self, instrument: dict, start_date: date, end_date: date) -> list[dict]:
        """Return sorted complete daily bars for inclusive [start_date, end_date]."""
        symbol, inst_tz, calendar, delay = self._validated_instrument(instrument)
        instrument_id = instrument["id"]
        if start_date > end_date:
            raise AppError(422, "invalid_date_range", "start_date must not be after end_date.")
        fetched_at = datetime.now(timezone.utc)
        try:
            frame = self._download(symbol, start_date, end_date)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(503, "provider_unavailable", self._safe_message(instrument_id)) from exc
        try:
            bars = self._parse_frame(
                frame=frame,
                instrument=instrument,
                symbol=symbol,
                calendar=calendar,
                inst_tz=inst_tz,
                delay=delay,
                start_date=start_date,
                end_date=end_date,
                fetched_at=fetched_at,
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(503, "provider_unavailable", self._safe_message(instrument_id)) from exc
        if not bars:
            raise AppError(503, "provider_unavailable", self._safe_message(instrument_id))
        return bars

    @staticmethod
    def _safe_message(instrument_id: str) -> str:
        return "Yahoo data is temporarily unavailable for " + str(instrument_id) + "."

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
        return (
            str(instrument["yahoo_symbol"]),
            str(instrument["timezone"]),
            str(instrument["calendar"]),
            delay,
        )

    def _download(self, symbol: str, start_date: date, end_date: date):
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        history = ticker.history
        kwargs: dict = {
            "start": start_date.isoformat(),
            "end": (end_date + timedelta(days=1)).isoformat(),
            "auto_adjust": False,
            "actions": True,
        }
        try:
            params = inspect.signature(history).parameters
        except (TypeError, ValueError):
            params = {}
        if "raise_errors" in params:
            kwargs["raise_errors"] = True
        frame = history(**kwargs)
        meta = self._history_metadata_once(ticker)
        try:
            if frame is not None and hasattr(frame, "attrs"):
                frame.attrs[_META_KEY] = meta
        except Exception:
            pass
        return frame

    @staticmethod
    def _history_metadata_once(ticker) -> dict:
        """Read cached history metadata once; never triggers extra network on its own."""
        try:
            meta = ticker.history_metadata
            if isinstance(meta, dict) and meta:
                return dict(meta)
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
        return dict(meta) if isinstance(meta, dict) else {}

    def _parse_frame(
        self,
        *,
        frame,
        instrument,
        symbol,
        calendar,
        inst_tz,
        delay,
        start_date,
        end_date,
        fetched_at,
    ) -> list[dict]:
        import pandas as pd

        instrument_id = str(instrument["id"])
        if frame is None or getattr(frame, "empty", True):
            return []
        self._validate_history_metadata(frame, instrument, symbol, inst_tz)
        df = frame.copy()
        cols = getattr(df, "columns", None)
        if cols is not None and isinstance(cols, pd.MultiIndex):
            df.columns = cols.get_level_values(0)
        norm: dict[str, str] = {}
        for col in list(df.columns):
            key = str(col).strip().lower().replace(" ", "_")
            norm[key] = str(col)

        def pick(*names: str):
            for name in names:
                if name in norm:
                    return norm[name]
            return None

        c_open = pick("open")
        c_high = pick("high")
        c_low = pick("low")
        c_close = pick("close")
        c_adj = pick("adj_close", "adjclose", "adjusted_close")
        c_vol = pick("volume")
        c_div = pick("dividends", "dividend")
        c_split = pick("stock_splits", "stock-split", "splits", "split")
        if c_open is None or c_high is None or c_low is None or c_close is None or c_adj is None:
            return []
        try:
            zone = ZoneInfo(inst_tz)
        except Exception as exc:
            raise AppError(422, "invalid_instrument", "Unknown timezone: " + inst_tz) from exc
        complete = set(complete_sessions(calendar, inst_tz, start_date, end_date, fetched_at))
        currency = str(instrument["currency"])
        source_url = "https://finance.yahoo.com/quote/" + symbol
        quality = "delayed" if delay > 0 else "ok"
        by_date: dict[str, dict] = {}
        for idx, row in df.iterrows():
            trading_day = self._row_date(idx, zone)
            if trading_day is None or trading_day < start_date or trading_day > end_date:
                continue
            if trading_day not in complete:
                continue
            try:
                o = self._req_positive_float(row.get(c_open))
                h = self._req_positive_float(row.get(c_high))
                lo = self._req_positive_float(row.get(c_low))
                c = self._req_positive_float(row.get(c_close))
                adj = self._req_positive_float(row.get(c_adj))
            except (TypeError, ValueError):
                continue
            if not (lo <= min(o, c) and max(o, c) <= h):
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
            try:
                event_time = session_close_utc(calendar, inst_tz, trading_day)
            except AppError:
                raise
            except Exception as exc:
                raise AppError(
                    503, "provider_unavailable", self._safe_message(instrument_id)
                ) from exc
            key = trading_day.isoformat()
            by_date[key] = {
                "instrument_id": instrument_id,
                "resolution": "1d",
                "date": key,
                "event_time": event_time,
                "fetched_at": fetched_at,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "adjusted_close": adj,
                "volume": volume,
                "dividends": dividends,
                "stock_splits": splits,
                "provider": "yahoo",
                "source_url": source_url,
                "currency": currency,
                "timezone": inst_tz,
                "delay_seconds": delay,
                "quality_status": quality,
            }
        return [by_date[k] for k in sorted(by_date)]

    def _validate_history_metadata(self, frame, instrument, symbol: str, inst_tz: str) -> None:
        """Cross-check cached Yahoo metadata; mismatch means provider_unavailable."""
        try:
            attrs = getattr(frame, "attrs", {}) or {}
            meta = attrs.get(_META_KEY, {})
        except Exception:
            meta = {}
        if not isinstance(meta, dict) or not meta:
            return
        instrument_id = str(instrument.get("id", "?"))

        def mismatch() -> AppError:
            return AppError(503, "provider_unavailable", self._safe_message(instrument_id))

        raw_symbol = meta.get("symbol", meta.get("ticker"))
        if isinstance(raw_symbol, str) and raw_symbol and raw_symbol.upper() != symbol.upper():
            raise mismatch()
        # Yahoo's `timezone` can be the abbreviation EDT/KST while
        # exchangeTimezoneName supplies the IANA zone. Compare the latter.
        raw_tz = meta.get("exchangeTimezoneName") or meta.get("timeZoneFullName")
        if not raw_tz and "/" in str(meta.get("timezone", "")):
            raw_tz = meta["timezone"]
        aliases = {"US/Eastern": "America/New_York"}
        if raw_tz and aliases.get(raw_tz, raw_tz) != inst_tz:
            raise mismatch()
        if str(instrument.get("currency", "POINT")).upper() != "POINT":
            raw_ccy = meta.get("currency", meta.get("priceCurrency"))
            if (
                isinstance(raw_ccy, str)
                and raw_ccy
                and raw_ccy.upper() != str(instrument["currency"]).upper()
            ):
                raise mismatch()

    @staticmethod
    def _row_date(idx, zone) -> date | None:
        try:
            import pandas as pd

            ts = pd.Timestamp(idx)
            if ts.tzinfo is not None:
                return ts.tz_convert(str(zone)).date()
            return ts.date()
        except Exception:
            return None

    @staticmethod
    def _req_positive_float(value) -> float:
        import pandas as pd

        if value is None or (isinstance(value, float) and pd.isna(value)):
            raise ValueError("missing")
        try:
            if pd.isna(value):
                raise ValueError("missing")
        except TypeError:
            pass
        num = float(value)
        if not math.isfinite(num) or num <= 0:
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
