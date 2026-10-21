from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_current_uid, get_db
from app.services.research import ResearchService

router = APIRouter(prefix="/api", tags=["research"], dependencies=[Depends(get_current_uid)])
DB = Annotated[object, Depends(get_db)]


@router.get("/news")
def news(
    db: DB,
    market: Literal["KR", "US"] = "US",
    instrument_id: str | None = Query(None, max_length=64),
    window: Literal["24h", "7d"] = "24h",
):
    return ResearchService(db).news(market, instrument_id, window)


@router.get("/analysis/{instrument_id}")
def analysis(db: DB, instrument_id: str, period: Literal["1y"] = "1y"):
    return ResearchService(db).analysis(instrument_id)


@router.get("/sentiment/us")
def sentiment(db: DB):
    return ResearchService(db).sentiment()


@router.get("/market/overview")
def overview(
    db: DB,
    instrument_id: str = Query("INDEX_SP500", max_length=64),
    window: Literal["24h", "7d"] = "24h",
):
    return ResearchService(db).overview(instrument_id, window)
