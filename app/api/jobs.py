from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.dependencies import get_current_uid, get_db
from app.services.conversation_deletion import ConversationDeletionService

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}")
def job_status(
    job_id: UUID,
    uid: Annotated[str, Depends(get_current_uid)],
    db: Annotated[object, Depends(get_db)],
):
    return ConversationDeletionService(db).status(uid, str(job_id))
