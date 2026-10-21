from datetime import date as Date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveValue = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataCreate(StrictModel):
    instrument_id: str = Field(min_length=1, max_length=80)
    date: Date
    value: PositiveValue
    memo: str = Field(default="", max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class DataUpdate(StrictModel):
    date: Date
    value: PositiveValue
    memo: str = Field(max_length=1000)
    expected_revision: int = Field(ge=1)


class DataImport(StrictModel):
    instrument_id: str = Field(min_length=1, max_length=80)
    start_date: Date
    end_date: Date

    @model_validator(mode="after")
    def range_valid(self):
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 730:
            raise ValueError("기간은 시작일 이상, 최대 2년이어야 합니다.")
        return self
