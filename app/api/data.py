from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from app.dependencies import get_current_uid, get_db
from app.schemas.data import DataCreate, DataImport, DataUpdate
from app.services.data import DataService
from app.services.market import MarketService

router = APIRouter(prefix="/api/data", tags=["data"])
UID = Annotated[str, Depends(get_current_uid)]
DB = Annotated[object, Depends(get_db)]
DocumentID = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


@router.post("", status_code=201)
def create_data(payload: DataCreate, uid: UID, db: DB):
    return DataService(db).create(uid, payload)


@router.get("")
def list_data(
    uid: UID,
    db: DB,
    instrument_id: str = "INDEX_SP500",
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=2048),
):
    return DataService(db).list(uid, instrument_id, start_date, end_date, limit, cursor)


@router.get("/summary")
def summarize_data(
    uid: UID,
    db: DB,
    instrument_id: str = "INDEX_SP500",
    start_date: date | None = None,
    end_date: date | None = None,
):
    return DataService(db).summary(uid, instrument_id, start_date, end_date)


@router.post("/import")
def import_data(payload: DataImport, uid: UID, db: DB):
    return MarketService(db).import_history(
        uid, payload.instrument_id, payload.start_date, payload.end_date
    )


@router.put("/{id}")
def update_data(id: DocumentID, payload: DataUpdate, uid: UID, db: DB):
    return DataService(db).update(uid, id, payload)


@router.delete("/{id}", status_code=204)
def delete_data(id: DocumentID, uid: UID, db: DB):
    DataService(db).delete(uid, id)
    return Response(status_code=204)
