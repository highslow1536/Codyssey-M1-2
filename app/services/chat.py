"""Chat orchestration for POST /api/chat.

Flow per request:
1. Verify model configuration BEFORE any Firestore write, so a missing
   key/model never creates turns or user messages (503).
2. Acquire the turn in a transaction (parent active_turn_id + turn lease)
   and save the user message BEFORE calling the model. A completed turn
   with the same body replays its saved response; the same request ID with
   a different body is rejected with 409.
3. Build server-side summary context from DataService for the requested
   instrument (default INDEX_SP500). Client summaries are never accepted
   (the request schema forbids them).
4. Call the model with bounded history and bounded tool rounds, then save
   the assistant output BEFORE returning the HTTP response. Completion is
   fenced by the attempt token so an expired in-flight attempt cannot
   overwrite a newer one.
5. On model failure the turn is marked failed; the user message stays and
   no assistant message is fabricated.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from app.errors import AppError
from app.repositories.conversations import ConversationRepository
from app.schemas.conversation import ChatRequest, ChatResponse

HISTORY_MESSAGE_LIMIT = 20
HISTORY_CONTENT_CHARS = 2000
SUMMARY_CONTEXT_CHARS = 6000
NEW_TITLE_CHARS = 40


def default_title_for(message: str) -> str:
    first_line = str(message).strip().splitlines()[0] if str(message).strip() else "새 대화"
    single = " ".join(first_line.split())
    if len(single) <= NEW_TITLE_CHARS:
        return single or "새 대화"
    return single[:NEW_TITLE_CHARS] + "..."


def summary_context_text(summary: dict[str, Any]) -> str:
    try:
        rendered = json.dumps(summary, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(summary)
    if len(rendered) > SUMMARY_CONTEXT_CHARS:
        return rendered[:SUMMARY_CONTEXT_CHARS] + "...(truncated)"
    return rendered


def sources_from_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    raw_urls = summary.get("source_urls")
    if isinstance(raw_urls, list):
        for url in raw_urls[:20]:
            if isinstance(url, str) and url:
                sources.append({"title": "", "url": url, "provider": ""})
    raw_sources = summary.get("sources")
    if isinstance(raw_sources, list):
        for entry in raw_sources[:20]:
            if isinstance(entry, dict):
                sources.append(
                    {
                        "title": str(entry.get("title", "")),
                        "url": str(entry.get("url", "")),
                        "provider": str(entry.get("provider", entry.get("source", ""))),
                    }
                )
    return sources


def data_as_of_from_summary(summary: dict[str, Any]) -> str:
    if not summary.get("count"):
        return ""
    for key in ("data_as_of", "end_date"):
        value = summary.get(key)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str) and value:
            return value
    return ""


def default_summary_fn(db: Any, owner_uid: str, instrument_id: str) -> dict[str, Any]:
    from app.services.data import DataService

    return DataService(db).summary(owner_uid, instrument_id)


def default_model_caller(db: Any, owner_uid: str) -> Callable[..., dict[str, Any]]:
    from app.providers import openai_client as client_module
    from app.tools import registry

    def call(
        *,
        settings: Any,
        instructions: str,
        history: list[dict[str, str]],
        user_message: str,
    ) -> dict[str, Any]:
        def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            return registry.execute_tool(db, owner_uid=owner_uid, name=name, arguments=args)

        return client_module.generate_reply(
            settings=settings,
            instructions=instructions,
            history=history,
            user_message=user_message,
            call_tool=run_tool,
        )

    return call


def handle_chat(
    db: Any,
    *,
    owner_uid: str,
    payload: ChatRequest,
    settings: Any | None = None,
    model_caller: Callable[..., dict[str, Any]] | None = None,
    summary_fn: Callable[[Any, str, str], dict[str, Any]] | None = None,
) -> ChatResponse:
    from app.providers.openai_client import build_instructions, ensure_model_configured

    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    ensure_model_configured(settings)

    try:
        from app.catalog import get_instrument

        get_instrument(payload.instrument_id)
    except AppError as exc:
        raise AppError(404, "instrument_not_found", exc.message) from exc

    repo = ConversationRepository(db)
    conversation_id = str(payload.conversation_id) if payload.conversation_id else None
    acquired = repo.acquire_turn(
        owner_uid,
        conversation_id,
        str(payload.client_request_id),
        payload.message,
        payload.instrument_id,
        int(getattr(settings, "chat_lease_seconds", 300) or 300),
        default_title_for(payload.message),
    )
    if acquired.get("replay"):
        saved = acquired["replay"] or {}
        return ChatResponse(
            conversation_id=acquired["conversation_id"],
            turn_id=acquired["turn_id"],
            message=str(saved.get("text", "")),
            sources=saved.get("sources") or [],
            tool_trace=saved.get("tool_trace") or [],
            data_as_of=str(saved.get("data_as_of", "")),
        )

    fetch_summary = summary_fn or default_summary_fn
    try:
        summary = fetch_summary(db, owner_uid, payload.instrument_id)
    except AppError as exc:
        repo.fail_turn(
            owner_uid,
            acquired["conversation_id"],
            acquired["turn_id"],
            acquired["attempt_token"],
            exc.code,
        )
        raise
    except Exception as exc:
        repo.fail_turn(
            owner_uid,
            acquired["conversation_id"],
            acquired["turn_id"],
            acquired["attempt_token"],
            "summary_failed",
        )
        raise AppError(502, "summary_failed", "요약 조회 실패: " + type(exc).__name__) from exc
    if not isinstance(summary, dict):
        repo.fail_turn(
            owner_uid,
            acquired["conversation_id"],
            acquired["turn_id"],
            acquired["attempt_token"],
            "summary_failed",
        )
        raise AppError(502, "summary_failed", "요약 결과가 객체가 아닙니다.")

    history_rows = repo.recent_messages(
        owner_uid, acquired["conversation_id"], HISTORY_MESSAGE_LIMIT
    )
    history: list[dict[str, str]] = []
    for row in history_rows:
        if row.get("turn_id") == acquired["turn_id"] and row.get("role") == "user":
            continue
        content = str(row.get("content", ""))
        if len(content) > HISTORY_CONTENT_CHARS:
            content = content[:HISTORY_CONTENT_CHARS] + "...(truncated)"
        history.append({"role": row.get("role", "user"), "content": content})

    instructions = build_instructions(summary_context_text(summary))
    caller = model_caller or default_model_caller(db, owner_uid)
    try:
        result = caller(
            settings=settings,
            instructions=instructions,
            history=history,
            user_message=payload.message,
        )
    except AppError as exc:
        repo.fail_turn(
            owner_uid,
            acquired["conversation_id"],
            acquired["turn_id"],
            acquired["attempt_token"],
            exc.code,
        )
        raise
    except Exception as exc:
        repo.fail_turn(
            owner_uid,
            acquired["conversation_id"],
            acquired["turn_id"],
            acquired["attempt_token"],
            "model_error",
        )
        raise AppError(502, "model_error", "모델 요청 실패: " + type(exc).__name__) from exc

    assistant_text = str((result or {}).get("text", "")).strip()
    if not assistant_text:
        repo.fail_turn(
            owner_uid,
            acquired["conversation_id"],
            acquired["turn_id"],
            acquired["attempt_token"],
            "model_error",
        )
        raise AppError(502, "model_error", "모델이 빈 답변을 반환했습니다.")
    tool_trace = (result or {}).get("tool_trace") or []
    sources = sources_from_summary(summary)
    data_as_of = data_as_of_from_summary(summary)

    repo.complete_turn(
        owner_uid,
        acquired["conversation_id"],
        acquired["turn_id"],
        acquired["attempt_token"],
        assistant_text,
        sources,
        tool_trace,
        data_as_of,
    )
    return ChatResponse(
        conversation_id=acquired["conversation_id"],
        turn_id=acquired["turn_id"],
        message=assistant_text,
        sources=sources,
        tool_trace=tool_trace,
        data_as_of=data_as_of,
    )
