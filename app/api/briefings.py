from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.config import get_settings
from app.dependencies import get_current_uid, get_db
from app.repositories.briefings import BriefingRepository
from app.schemas.briefing import BriefingSelection, ScheduleCreate, ScheduleUpdate, SendBriefing
from app.services.briefings import BriefingService

router = APIRouter(prefix="/api", tags=["briefings"])
DB = Annotated[object, Depends(get_db)]
UID = Annotated[str, Depends(get_current_uid)]


@router.get("/briefing-schedules")
def schedules(db: DB, uid: UID):
    return {
        "items": BriefingRepository(db).list_schedules(uid),
        "discord_configured": bool(get_settings().discord_webhook_url),
    }


@router.post("/briefing-schedules", status_code=201)
def create_schedule(body: ScheduleCreate, db: DB, uid: UID):
    return BriefingRepository(db).create_schedule(
        uid, body.model_dump(), datetime.now(timezone.utc)
    )


@router.patch("/briefing-schedules/{schedule_id}")
def update_schedule(schedule_id: str, body: ScheduleUpdate, db: DB, uid: UID):
    return BriefingRepository(db).update_schedule(
        uid, schedule_id, body.model_dump(), datetime.now(timezone.utc)
    )


@router.delete("/briefing-schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: str, db: DB, uid: UID):
    BriefingRepository(db).delete_schedule(uid, schedule_id, datetime.now(timezone.utc))
    return Response(status_code=204)


@router.post("/briefings/preview", status_code=201)
def preview(body: BriefingSelection, db: DB, uid: UID):
    return BriefingService(db).preview(uid, body.model_dump())


@router.post("/briefings/{briefing_id}/send")
def send(briefing_id: str, body: SendBriefing, db: DB, uid: UID):
    return BriefingService(db).queue(uid, briefing_id, body.idempotency_key)


@router.get("/briefings")
def history(
    db: DB,
    uid: UID,
    limit: int = Query(20, ge=1, le=50),
    cursor: str | None = Query(None, max_length=2048),
):
    return BriefingRepository(db).list(uid, limit, cursor)


@router.get("/briefings/{briefing_id}")
def detail(briefing_id: str, db: DB, uid: UID):
    return BriefingRepository(db).get(uid, briefing_id)
