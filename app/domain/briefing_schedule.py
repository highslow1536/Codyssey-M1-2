"""Briefing schedule solver: next run strictly after a reference instant."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.domain.sessions import session_close_utc, trading_sessions
from app.errors import AppError

MARKET_CALENDARS = {
    "KR": ("XKRX", "Asia/Seoul"),
    "US": ("XNYS", "America/New_York"),
}
HORIZON_DAYS = 370
CHUNK_DAYS = 14
DEFAULT_WEEKDAYS = (0, 1, 2, 3, 4)
DEFAULT_CLOSE_OFFSET_MINUTES = 40
MAX_CLOSE_OFFSET_MINUTES = 180

__all__ = ["next_run", "MARKET_CALENDARS", "HORIZON_DAYS"]


def _invalid(message: str) -> AppError:
    return AppError(422, "briefing_invalid_schedule", message)


def _parse_schedule(schedule: dict) -> dict:
    if not isinstance(schedule, dict):
        raise _invalid("schedule must be a dict.")
    market = schedule.get("market")
    if market not in ("KR", "US"):
        raise _invalid("market must be KR or US.")
    kind = schedule.get("schedule_kind")
    if kind not in ("market_close", "fixed_time"):
        raise _invalid("schedule_kind must be market_close or fixed_time.")
    offset = schedule.get("close_offset_minutes", DEFAULT_CLOSE_OFFSET_MINUTES)
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise _invalid("close_offset_minutes must be an int.")
    if not 0 <= offset <= MAX_CLOSE_OFFSET_MINUTES:
        raise _invalid("close_offset_minutes must be 0..180.")
    parsed: dict = {"market": market, "schedule_kind": kind, "offset": offset}
    tz_name = schedule.get("timezone")
    if kind == "fixed_time":
        if not isinstance(tz_name, str) or not tz_name.strip():
            raise _invalid("timezone is required for fixed_time.")
        try:
            parsed["tz"] = ZoneInfo(tz_name.strip())
        except Exception as exc:
            raise _invalid("timezone is not a valid IANA name.") from exc
        hour, minute = _parse_local_time(schedule.get("local_time"))
        parsed["hour"] = hour
        parsed["minute"] = minute
        parsed["weekdays"] = _parse_weekdays(schedule.get("weekdays"))
    elif tz_name is not None:
        if not isinstance(tz_name, str) or not tz_name.strip():
            raise _invalid("timezone must be a valid IANA name.")
        try:
            ZoneInfo(tz_name.strip())
        except Exception as exc:
            raise _invalid("timezone is not a valid IANA name.") from exc
    return parsed


def _parse_local_time(value: object) -> tuple[int, int]:
    if not isinstance(value, str):
        raise _invalid("local_time must be HH:MM.")
    parts = value.strip().split(":")
    if len(parts) != 2 or len(parts[0].strip()) != 2 or len(parts[1].strip()) != 2:
        raise _invalid("local_time must be HH:MM.")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise _invalid("local_time must be HH:MM.") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise _invalid("local_time must be HH:MM.")
    return hour, minute


def _parse_weekdays(value: object) -> tuple[int, ...]:
    if value is None:
        return DEFAULT_WEEKDAYS
    if not isinstance(value, (list, tuple)):
        raise _invalid("weekdays must be a list of ints 0..6.")
    days: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise _invalid("weekdays must be a list of ints 0..6.")
        if not 0 <= item <= 6:
            raise _invalid("weekdays must be a list of ints 0..6.")
        if item not in days:
            days.append(item)
    if not days:
        raise _invalid("weekdays must not be empty.")
    return tuple(days)


def _require_after(after: datetime) -> datetime:
    if not isinstance(after, datetime):
        raise AppError(422, "briefing_invalid_after", "after must be aware.")
    if after.tzinfo is None or after.utcoffset() is None:
        raise AppError(422, "briefing_invalid_after", "after must be aware.")
    return after.astimezone(timezone.utc)


def next_run(schedule: dict, after: datetime) -> datetime:
    """Return the next run as an aware UTC datetime strictly after input."""
    parsed = _parse_schedule(schedule)
    after_utc = _require_after(after)
    if parsed["schedule_kind"] == "market_close":
        return _next_market_close(parsed, after_utc)
    return _next_fixed_time(parsed, after_utc)


def _next_market_close(parsed: dict, after_utc: datetime) -> datetime:
    calendar, market_tz = MARKET_CALENDARS[parsed["market"]]
    zone = ZoneInfo(market_tz)
    offset = timedelta(minutes=parsed["offset"])
    start = after_utc.astimezone(zone).date()
    deadline = start + timedelta(days=HORIZON_DAYS)
    cursor = start
    while cursor <= deadline:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS - 1), deadline)
        for session in trading_sessions(calendar, cursor, chunk_end):
            fire = session_close_utc(calendar, market_tz, session) + offset
            if fire > after_utc:
                return fire
        cursor = chunk_end + timedelta(days=1)
    raise AppError(422, "briefing_no_upcoming_run", "No run found in horizon.")


def _is_nonexistent(zone: ZoneInfo, y: int, m: int, d: int, hh: int, mm: int) -> bool:
    probe = datetime(y, m, d, hh, mm, tzinfo=zone, fold=0)
    back = probe.astimezone(timezone.utc).astimezone(zone)
    actual = (back.year, back.month, back.day, back.hour, back.minute)
    return actual != (y, m, d, hh, mm)


def _next_fixed_time(parsed: dict, after_utc: datetime) -> datetime:
    zone: ZoneInfo = parsed["tz"]
    hour, minute = parsed["hour"], parsed["minute"]
    weekdays = set(parsed["weekdays"])
    base = after_utc.astimezone(zone).date()
    for delta in range(HORIZON_DAYS + 1):
        day = base + timedelta(days=delta)
        if day.weekday() not in weekdays:
            continue
        if _is_nonexistent(zone, day.year, day.month, day.day, hour, minute):
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute)
        candidate = candidate.replace(tzinfo=zone, fold=0)
        fire = candidate.astimezone(timezone.utc)
        if fire > after_utc:
            return fire
    raise AppError(422, "briefing_no_upcoming_run", "No run found in horizon.")
