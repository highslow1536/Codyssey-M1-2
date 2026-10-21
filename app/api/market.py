from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.catalog import INSTRUMENTS
from app.dependencies import get_current_uid, get_db
from app.services.market import MarketService

router = APIRouter(prefix="/api", tags=["market"], dependencies=[Depends(get_current_uid)])


@router.get("/instruments")
def instruments(query: str = Query("", max_length=100), market: Literal["KR", "US"] | None = None):
    return {
        "items": [
            item
            for item in INSTRUMENTS.values()
            if (market is None or item["market"] == market)
            and query.lower() in f"{item['id']} {item['name']} {item['yahoo_symbol']}".lower()
        ]
    }


@router.get("/market/{instrument_id}/history")
def history(
    instrument_id: str,
    db: Annotated[object, Depends(get_db)],
    start_date: date | None = None,
    end_date: date | None = None,
):
    return MarketService(db).history(instrument_id, start_date, end_date)
