from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field

from app.config import get_settings
from app.dependencies import get_current_uid, get_db
from app.repositories.alerts import AlertRepository
from app.schemas.alert import AlertCreate, AlertUpdate, AlertValues
from app.schemas.data import StrictModel
from app.services.alerts import AlertService, preview

router = APIRouter(prefix="/api", tags=["price-alerts"])
DB = Annotated[object, Depends(get_db)]
UID = Annotated[str, Depends(get_current_uid)]


class ResetAlert(StrictModel):
    expected_revision: int = Field(ge=1)


class RetryAlert(StrictModel):
    idempotency_key: str = Field(min_length=1, max_length=128)


@router.get("/alerts")
def rules(db: DB, uid: UID):
    return {
        "items": AlertRepository(db).list_rules(uid),
        "discord_configured": bool(get_settings().discord_webhook_url),
    }


@router.post("/alerts/preview")
def rule_preview(body: AlertValues, uid: UID):
    return preview(body.model_dump())


@router.post("/alerts", status_code=201)
def create(body: AlertCreate, db: DB, uid: UID):
    return AlertRepository(db).create(uid, body.model_dump(), datetime.now(timezone.utc))


@router.patch("/alerts/{alert_id}")
def update(alert_id: str, body: AlertUpdate, db: DB, uid: UID):
    return AlertRepository(db).update(uid, alert_id, body.model_dump(), datetime.now(timezone.utc))


@router.post("/alerts/{alert_id}/reset")
def reset(alert_id: str, body: ResetAlert, db: DB, uid: UID):
    return AlertRepository(db).reset(
        uid, alert_id, body.expected_revision, datetime.now(timezone.utc)
    )


@router.delete("/alerts/{alert_id}", status_code=204)
def delete(alert_id: str, db: DB, uid: UID):
    AlertRepository(db).delete(uid, alert_id, datetime.now(timezone.utc))
    return Response(status_code=204)


@router.get("/alert-events")
def events(
    db: DB,
    uid: UID,
    limit: int = Query(20, ge=1, le=50),
    cursor: str | None = Query(None, max_length=2048),
):
    return AlertRepository(db).list_events(uid, limit, cursor)


@router.get("/alert-events/{event_id}")
def detail(event_id: str, db: DB, uid: UID):
    return AlertRepository(db).get(uid, event_id)


@router.post("/alert-events/{event_id}/retry")
def retry(event_id: str, body: RetryAlert, db: DB, uid: UID):
    return AlertService(db).retry(uid, event_id, body.idempotency_key)
