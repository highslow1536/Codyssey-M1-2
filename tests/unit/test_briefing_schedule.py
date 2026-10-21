"""Schedule solver tests: holidays, early close, DST, nonexistent slots."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.briefing_schedule import next_run
from app.errors import AppError

UTC = timezone.utc
KR_CLOSE = {"market": "KR", "schedule_kind": "market_close"}
US_CLOSE = {"market": "US", "schedule_kind": "market_close"}


def test_kr_close_same_day_plus_default_offset():
    after = datetime(2024, 1, 8, 5, 0, tzinfo=UTC)
    assert next_run(KR_CLOSE, after) == datetime(2024, 1, 8, 7, 10, tzinfo=UTC)


def test_strictly_after_equal_fire_moves_to_next_session():
    after = datetime(2024, 1, 8, 7, 10, tzinfo=UTC)
    assert next_run(KR_CLOSE, after) == datetime(2024, 1, 9, 7, 10, tzinfo=UTC)


def test_custom_offset_applies():
    sched = {"market": "KR", "schedule_kind": "market_close", "close_offset_minutes": 0}
    after = datetime(2024, 1, 8, 5, 0, tzinfo=UTC)
    assert next_run(sched, after) == datetime(2024, 1, 8, 6, 30, tzinfo=UTC)


def test_us_thanksgiving_skipped_early_close_used():
    after = datetime(2024, 11, 27, 22, 0, tzinfo=UTC)
    assert next_run(US_CLOSE, after) == datetime(2024, 11, 29, 18, 40, tzinfo=UTC)


def test_us_dst_summer_and_winter_closes():
    summer = datetime(2024, 7, 1, 19, 0, tzinfo=UTC)
    assert next_run(US_CLOSE, summer) == datetime(2024, 7, 1, 20, 40, tzinfo=UTC)
    winter = datetime(2024, 1, 8, 20, 0, tzinfo=UTC)
    assert next_run(US_CLOSE, winter) == datetime(2024, 1, 8, 21, 40, tzinfo=UTC)


def test_current_date_regression_both_markets():
    now = datetime(2026, 10, 22, 7, 0, tzinfo=UTC)
    for sched in (KR_CLOSE, US_CLOSE):
        fire = next_run(sched, now)
        assert fire.tzinfo is not None and fire.utcoffset() is not None
        assert fire > now


def test_calendar_boundary_stays_fail_closed():
    now = datetime(2026, 10, 22, 7, 0, tzinfo=UTC)
    with pytest.raises(AppError):
        next_run({"market": "XX", "schedule_kind": "market_close"}, now)


def test_fixed_time_basic_and_weekend_skip():
    sched = {
        "market": "KR",
        "schedule_kind": "fixed_time",
        "timezone": "Asia/Seoul",
        "local_time": "09:00",
    }
    friday = datetime(2024, 1, 12, 0, 30, tzinfo=UTC)
    assert next_run(sched, friday) == datetime(2024, 1, 15, 0, 0, tzinfo=UTC)


def test_fixed_time_nonexistent_dst_slot_skipped():
    sched = {
        "market": "US",
        "schedule_kind": "fixed_time",
        "timezone": "America/New_York",
        "local_time": "02:30",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
    }
    after = datetime(2024, 3, 10, 6, 0, tzinfo=UTC)
    assert next_run(sched, after) == datetime(2024, 3, 11, 6, 30, tzinfo=UTC)


def test_fixed_time_ambiguous_slot_fires_once():
    sched = {
        "market": "US",
        "schedule_kind": "fixed_time",
        "timezone": "America/New_York",
        "local_time": "01:30",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
    }
    first = next_run(sched, datetime(2024, 11, 3, 4, 0, tzinfo=UTC))
    assert first == datetime(2024, 11, 3, 5, 30, tzinfo=UTC)
    second = next_run(sched, first)
    assert second == datetime(2024, 11, 4, 6, 30, tzinfo=UTC)


def test_invalid_schedules_fail():
    now = datetime(2024, 1, 8, 5, 0, tzinfo=UTC)
    bad = [
        {"market": "KR"},
        {"market": "XX", "schedule_kind": "market_close"},
        {"market": "KR", "schedule_kind": "market_close", "close_offset_minutes": 999},
        {
            "market": "KR",
            "schedule_kind": "fixed_time",
            "timezone": "Asia/Seoul",
            "local_time": "25:00",
        },
        {
            "market": "KR",
            "schedule_kind": "fixed_time",
            "timezone": "Nope/Zone",
            "local_time": "09:00",
        },
        {
            "market": "KR",
            "schedule_kind": "fixed_time",
            "timezone": "Asia/Seoul",
            "local_time": "09:00",
            "weekdays": [],
        },
    ]
    for sched in bad:
        with pytest.raises(AppError):
            next_run(sched, now)


def test_naive_after_fails():
    with pytest.raises(AppError):
        next_run(KR_CLOSE, datetime(2024, 1, 8, 5, 0))


def test_result_is_aware_utc_strictly_after():
    after = datetime(2024, 1, 8, 5, 0, tzinfo=UTC)
    fire = next_run(KR_CLOSE, after)
    assert fire.tzname() == "UTC"
    assert fire > after
