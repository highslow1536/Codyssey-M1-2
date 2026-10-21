"""Conversation + chat schemas (bounded slice).

Rules enforced here:
- Conversation creation takes an explicit title plus an optional list of at
  most 100 user/assistant messages (role whitelist).
- The owner UID is never accepted from client input; it is injected
  server-side from the auth dependency. All request models forbid extras.
- Chat requests carry no client-side summary; the server always builds
  summary context from DataService.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

RoleLiteral = Literal["user", "assistant"]

DEFAULT_INSTRUMENT_ID = "INDEX_SP500"

MAX_CHAT_MESSAGE_LEN = 4000
MAX_CREATE_MESSAGES = 100
MAX_TITLE_LEN = 200


def _strip_nonempty(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(field_name + " must not be blank")
    return cleaned


class ConversationMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: RoleLiteral
    content: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_LEN)

    @field_validator("content")
    @classmethod
    def _clean_content(cls, v: str) -> str:
        return _strip_nonempty(v, "content")


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_LEN)
    messages: list[ConversationMessageIn] = Field(
        default_factory=list, max_length=MAX_CREATE_MESSAGES
    )
    idempotency_key: UUID | None = None

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str) -> str:
        return _strip_nonempty(v, "title")


class ChatRequest(BaseModel):
    """POST /api/chat request body."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_LEN)
    conversation_id: UUID | None = None
    client_request_id: UUID
    instrument_id: str = Field(default=DEFAULT_INSTRUMENT_ID, min_length=1, max_length=64)

    @field_validator("message")
    @classmethod
    def _clean_message(cls, v: str) -> str:
        return _strip_nonempty(v, "message")

    @field_validator("instrument_id")
    @classmethod
    def _clean_instrument(cls, v: str) -> str:
        return _strip_nonempty(v, "instrument_id")


class ConversationSummary(BaseModel):
    id: str
    title: str
    message_count: int = 0
    last_message_preview: str = ""
    updated_at: str = ""
    revision: int = 1


class ConversationListOut(BaseModel):
    items: list[ConversationSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class ToolTraceItem(BaseModel):
    tool: str
    ok: bool = True
    summary: str = ""


class SourceItem(BaseModel):
    title: str = ""
    url: str = ""
    provider: str = ""


class ChatMessageOut(BaseModel):
    id: str = ""
    role: RoleLiteral
    content: str
    sequence: int = 0
    created_at: str = ""
    turn_id: str | None = None
    sources: list[SourceItem] = Field(default_factory=list)
    tool_trace: list[ToolTraceItem] = Field(default_factory=list)
    data_as_of: str = ""


class ConversationDetail(BaseModel):
    id: str
    title: str
    message_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    revision: int = 1
    messages: list[ChatMessageOut] = Field(default_factory=list)
    next_cursor: str | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    turn_id: str
    message: str
    sources: list[SourceItem] = Field(default_factory=list)
    tool_trace: list[ToolTraceItem] = Field(default_factory=list)
    data_as_of: str = ""
