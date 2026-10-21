"""Conversation CRUD router.

All routes are sync def and require Firebase auth (get_current_uid).
get_db provides the sync Firestore client.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse

from app.dependencies import get_current_uid, get_db
from app.repositories.conversations import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_MESSAGE_LIMIT,
    MAX_LIST_LIMIT,
    MAX_MESSAGE_LIMIT,
    ConversationRepository,
)
from app.schemas.conversation import (
    ChatMessageOut,
    ConversationCreate,
    ConversationDetail,
    ConversationListOut,
    ConversationSummary,
)
from app.services.conversation_deletion import ConversationDeletionService

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

CREATE_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

SWEEP_ITERATIONS = 10


def _repo(db: Any) -> ConversationRepository:
    return ConversationRepository(db)


@router.post("", status_code=201)
@router.post("/", status_code=201, include_in_schema=False)
def create_conversation(
    payload: ConversationCreate,
    response: Response,
    db: Any = Depends(get_db),
    uid: str = Depends(get_current_uid),
) -> ConversationDetail:
    repo = _repo(db)
    if payload.idempotency_key:
        doc_id = str(uuid5(CREATE_NAMESPACE, uid + ":conversation:" + str(payload.idempotency_key)))
    else:
        doc_id = str(uuid4())
    meta, created = repo.create(
        uid,
        payload.title,
        [item.model_dump() for item in payload.messages],
        conversation_id=doc_id,
        idempotency_key=str(payload.idempotency_key) if payload.idempotency_key else None,
    )
    if not created:
        response.status_code = 200
    page = repo.messages(uid, meta["id"], limit=MAX_CREATE_MESSAGES_LIMIT, cursor=None)
    return ConversationDetail(
        id=meta["id"],
        title=meta["title"],
        message_count=meta["message_count"],
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        revision=meta.get("revision", 1),
        messages=[ChatMessageOut(**m) for m in page["messages"]],
        next_cursor=page["next_cursor"],
    )


MAX_CREATE_MESSAGES_LIMIT = 100


@router.get("", response_model=ConversationListOut)
@router.get("/", response_model=ConversationListOut, include_in_schema=False)
def list_conversations(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    cursor: str | None = Query(default=None),
    db: Any = Depends(get_db),
    uid: str = Depends(get_current_uid),
) -> ConversationListOut:
    result = _repo(db).list(uid, limit=limit, cursor=cursor)
    return ConversationListOut(
        items=[ConversationSummary(**item) for item in result["items"]],
        next_cursor=result["next_cursor"],
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: UUID,
    limit: int = Query(default=DEFAULT_MESSAGE_LIMIT, ge=1, le=MAX_MESSAGE_LIMIT),
    cursor: str | None = Query(default=None),
    db: Any = Depends(get_db),
    uid: str = Depends(get_current_uid),
) -> ConversationDetail:
    repo = _repo(db)
    meta = repo.get(uid, str(conversation_id))
    page = repo.messages(uid, str(conversation_id), limit=limit, cursor=cursor)
    return ConversationDetail(
        id=meta["id"],
        title=meta["title"],
        message_count=meta["message_count"],
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        revision=meta.get("revision", 1),
        messages=[ChatMessageOut(**m) for m in page["messages"]],
        next_cursor=page["next_cursor"],
    )


@router.delete("/{conversation_id}")
def delete_conversation(
    conversation_id: UUID,
    db: Any = Depends(get_db),
    uid: str = Depends(get_current_uid),
) -> Response:
    result = ConversationDeletionService(db).start(uid, str(conversation_id), SWEEP_ITERATIONS)
    if result["status"] == "completed":
        return Response(status_code=204)
    return JSONResponse(status_code=202, content=result)
