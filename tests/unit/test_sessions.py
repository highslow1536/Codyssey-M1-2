"""Calendar tests: sessions, early close, still-open filtering, fail-closed."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from app.domain.sessions import (
    _require_calendar,
    complete_sessions,
    is_complete_session,
    session_close_utc,
    trading_sessions,
)
from app.errors import AppError


def test_vix_uses_its_own_regular_close_including_early_close():
    from app.catalog import get_instrument

    instrument = get_instrument("INDEX_VIX")
    close = session_close_utc(instrument["calendar"], instrument["timezone"], date(2026, 10, 20))
    early = session_close_utc(instrument["calendar"], instrument["timezone"], date(2025, 11, 28))
    assert (close.hour, close.minute) == (20, 15)
    assert (early.hour, early.minute) == (18, 15)


def test_kr_weekday_sessions_exclude_weekend():
    sessions = trading_sessions("XKRX", date(2024, 1, 8), date(2024, 1, 14))
    assert date(2024, 1, 8) in sessions  # Monday
    assert date(2024, 1, 12) in sessions  # Friday
    assert date(2024, 1, 6) not in sessions
    assert date(2024, 1, 7) not in sessions
    assert date(2024, 1, 13) not in sessions
    assert date(2024, 1, 14) not in sessions


def test_us_early_close_counts_as_session():
    # 2024-11-28 Thanksgiving (closed), 2024-11-29 early close (open).
    sessions = trading_sessions("XNYS", date(2024, 11, 27), date(2024, 11, 29))
    assert date(2024, 11, 29) in sessions
    assert date(2024, 11, 28) not in sessions
    close_utc = session_close_utc("XNYS", "America/New_York", date(2024, 11, 29))
    assert close_utc.tzinfo is not None
    assert close_utc.utcoffset() is not None
    # Early close 13:00 ET = 18:00 UTC (EST). Regular close would be 21:00 UTC.
    assert (close_utc.hour, close_utc.minute) == (18, 0)


def test_session_close_utc_is_aware_utc():
    close_utc = session_close_utc("XKRX", "Asia/Seoul", date(2024, 1, 8))
    assert close_utc.tzinfo is not None
    assert close_utc.tzname() == "UTC"
    # 15:30 KST = 06:30 UTC.
    assert (close_utc.hour, close_utc.minute) == (6, 30)


def test_complete_sessions_excludes_still_open_day():
    now = datetime(2024, 1, 10, 5, 0, tzinfo=timezone.utc)  # before KR close
    done = complete_sessions("XKRX", "Asia/Seoul", date(2024, 1, 8), date(2024, 1, 10), now)
    assert date(2024, 1, 8) in done
    assert date(2024, 1, 9) in done
    assert date(2024, 1, 10) not in done
    assert is_complete_session("XKRX", "Asia/Seoul", date(2024, 1, 8), now) is True
    assert is_complete_session("XKRX", "Asia/Seoul", date(2024, 1, 10), now) is False


def test_complete_sessions_excludes_future_range():
    now = datetime(2024, 1, 12, 12, 0, tzinfo=timezone.utc)
    done = complete_sessions("XKRX", "Asia/Seoul", date(2024, 1, 8), date(2024, 1, 30), now)
    assert done
    assert max(done) <= date(2024, 1, 12)
    assert date(2024, 1, 13) not in done


def test_unknown_calendar_fails_closed():
    with pytest.raises(AppError) as exc:
        trading_sessions("NO_SUCH_CAL", date(2024, 1, 8), date(2024, 1, 10))
    assert exc.value.status_code == 503
    assert exc.value.code == "calendar_unavailable"
    with pytest.raises(AppError) as exc:
        session_close_utc("NO_SUCH_CAL", "Asia/Seoul", date(2024, 1, 8))
    assert exc.value.code == "calendar_unavailable"
    with pytest.raises(AppError) as exc:
        complete_sessions("NO_SUCH_CAL", "Asia/Seoul", date(2024, 1, 8), date(2024, 1, 10))
    assert exc.value.code == "calendar_unavailable"


def test_out_of_range_dates_fail_closed():
    # XKRX data starts 2006-10-23; 1990 is outside calendar bounds.
    with pytest.raises(AppError) as exc:
        trading_sessions("XKRX", date(1990, 1, 1), date(1990, 1, 10))
    assert exc.value.status_code == 503
    assert exc.value.code == "calendar_unavailable"


def test_non_session_close_fails_closed():
    # 2024-01-06 is a Saturday, never a session close.
    with pytest.raises(AppError) as exc:
        session_close_utc("XKRX", "Asia/Seoul", date(2024, 1, 6))
    assert exc.value.status_code == 503
    assert exc.value.code == "calendar_unavailable"


def test_sdk_unavailable_fails_closed():
    _require_calendar.cache_clear()
    try:
        with patch.dict(sys.modules, {"exchange_calendars": None}):
            with pytest.raises(AppError) as exc:
                trading_sessions("XKRX", date(2024, 1, 8), date(2024, 1, 10))
        assert exc.value.status_code == 503
        assert exc.value.code == "calendar_unavailable"
    finally:
        _require_calendar.cache_clear()
