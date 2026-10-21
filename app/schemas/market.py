"""Market response schemas for the bounded Yahoo slice."""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class DailyBar(BaseModel):
    """One complete daily bar with provenance metadata."""

    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(min_length=1)
    resolution: Literal["1d"] = "1d"
    date: _date
    event_time: datetime
    fetched_at: datetime
    open: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    close: float = Field(gt=0, allow_inf_nan=False)
    adjusted_close: float = Field(gt=0, allow_inf_nan=False)
    volume: Optional[int] = Field(default=None, ge=0)
    dividends: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    stock_splits: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    provider: Literal["yahoo"] = "yahoo"
    source_url: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    delay_seconds: int = Field(ge=0)
    quality_status: str = Field(min_length=1)
