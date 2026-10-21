"""Chat router: POST /api/chat (sync def, auth required)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.dependencies import get_current_uid, get_db
from app.schemas.conversation import ChatRequest, ChatResponse
from app.services.chat import handle_chat

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
@router.post("/", response_model=ChatResponse, include_in_schema=False)
def post_chat(
    payload: ChatRequest,
    db: Any = Depends(get_db),
    uid: str = Depends(get_current_uid),
) -> ChatResponse:
    return handle_chat(db, owner_uid=uid, payload=payload, settings=get_settings())
