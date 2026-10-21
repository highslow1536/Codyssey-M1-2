"""Exchange calendar helpers: sessions and UTC close times (fail-closed)."""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

from app.errors import AppError


def _calendar_unavailable(detail: str) -> AppError:
    return AppError(
        503, "calendar_unavailable", "Trading calendar is temporarily unavailable: " + detail + "."
    )


@lru_cache(maxsize=16)
def _require_calendar(calendar_name: str):
    """Return the exchange calendar or raise 503 when SDK/calendar is unusable."""
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise _calendar_unavailable(str(calendar_name)) from exc
    try:
        return xcals.get_calendar(calendar_name)
    except Exception as exc:
        raise _calendar_unavailable(str(calendar_name)) from exc


def trading_sessions(calendar_name: str, start: date, end: date) -> list[date]:
    """All trading sessions in [start, end] inclusive (early closes included)."""
    if start > end:
        return []
    cal = _require_calendar(calendar_name)
    try:
        idx = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    except AppError:
        raise
    except Exception as exc:
        raise _calendar_unavailable(str(calendar_name)) from exc
    out: list[date] = []
    for d in idx:
        try:
            out.append(d.date())
        except AttributeError:
            out.append(date(d.year, d.month, d.day))
    return out


def session_close_utc(calendar_name: str, timezone_name: str, session: date) -> datetime:
    """Actual session close for the given trading day as aware UTC datetime."""
    from datetime import timezone as dt_timezone

    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise _calendar_unavailable(str(calendar_name)) from exc
    cal = _require_calendar(calendar_name)
    try:
        sched = cal.schedule
        ts = pd.Timestamp(session)
        if ts not in sched.index:
            raise _calendar_unavailable(str(calendar_name))
        close = sched.loc[ts, "close"]
        if getattr(close, "tzinfo", None) is None:
            raise _calendar_unavailable(str(calendar_name))
        return pd.Timestamp(close).tz_convert("UTC").to_pydatetime().replace(tzinfo=dt_timezone.utc)
    except AppError:
        raise
    except Exception as exc:
        raise _calendar_unavailable(str(calendar_name)) from exc


def is_complete_session(
    calendar_name: str, timezone_name: str, session: date, now: datetime | None = None
) -> bool:
    """True when the session close has passed (early-close aware)."""
    from datetime import timezone as dt_timezone

    ref = now or datetime.now(dt_timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=dt_timezone.utc)
    return session_close_utc(calendar_name, timezone_name, session) <= ref


def complete_sessions(
    calendar_name: str,
    timezone_name: str,
    start: date,
    end: date,
    now: datetime | None = None,
) -> list[date]:
    """Sessions in range whose close has passed; excludes the still-open day."""
    return [
        s
        for s in trading_sessions(calendar_name, start, end)
        if is_complete_session(calendar_name, timezone_name, s, now)
    ]
