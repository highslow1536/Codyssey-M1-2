from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from app.catalog import get_instrument
from app.schemas.data import StrictModel


class BriefingSelection(StrictModel):
    market: Literal["KR", "US"]
    instrument_ids: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def instruments_valid(self):
        if len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise ValueError("관심 종목은 중복할 수 없습니다.")
        for instrument_id in self.instrument_ids:
            if get_instrument(instrument_id)["market"] != self.market:
                raise ValueError("관심 종목과 시장이 일치해야 합니다.")
        return self


class ScheduleValues(BriefingSelection):
    schedule_kind: Literal["market_close", "fixed_time"] = "market_close"
    timezone: str = Field(default="Asia/Seoul", max_length=80)
    local_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] = Field(default_factory=lambda: list(range(5)), min_length=1, max_length=7)
    close_offset_minutes: int = Field(default=40, ge=0, le=180)
    active: bool = True
    destination_alias: Literal["default"] = "default"

    @model_validator(mode="after")
    def time_valid(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("IANA 시간대를 확인하세요.") from exc
        if any(day < 0 or day > 6 for day in self.weekdays):
            raise ValueError("요일은 월요일 0부터 일요일 6까지입니다.")
        self.weekdays = sorted(set(self.weekdays))
        if self.schedule_kind == "market_close":
            self.timezone = "Asia/Seoul" if self.market == "KR" else "America/New_York"
            self.local_time = None
        elif not self.local_time:
            raise ValueError("지정 시각이 필요합니다.")
        return self


class ScheduleCreate(ScheduleValues):
    idempotency_key: str = Field(min_length=1, max_length=128)


class ScheduleUpdate(ScheduleValues):
    expected_revision: int = Field(ge=1)


class SendBriefing(StrictModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
