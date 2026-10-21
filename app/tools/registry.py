"""Allowlisted chat tool registry.

Only summarize_dataset is exposed to the model. Arguments are strictly
validated with Pydantic (extra fields forbidden); there is deliberately no
owner-UID field. The owner is always injected server-side.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_TOOL_NAMES = ("summarize_dataset",)

_SUMMARY_TOOL_DESCRIPTION = (
    "Return the server-side summary of the user's stored dataset for one "
    "instrument and optional date range. Use this instead of guessing numbers."
)


class SummarizeDatasetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(min_length=1, max_length=64, pattern="^[A-Za-z0-9_.-]+$")
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("instrument_id")
    @classmethod
    def _clean_instrument(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("instrument_id must not be blank")
        return cleaned


def _owner_like_keys(raw: dict[str, Any]) -> list[str]:
    found = []
    for key in raw:
        lowered = key.lower()
        if "owner" in lowered or lowered in {"uid", "user_id", "user"}:
            found.append(key)
    return found


def summarize_dataset_tool_definition() -> dict[str, Any]:
    """OpenAI Responses-API function-tool definition (strict, no owner field)."""
    return {
        "type": "function",
        "name": "summarize_dataset",
        "description": _SUMMARY_TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "instrument_id": {
                    "type": "string",
                    "description": "Instrument ID from the catalog, e.g. INDEX_SP500",
                },
                "start_date": {
                    "type": ["string", "null"],
                    "description": "Start date YYYY-MM-DD or null",
                },
                "end_date": {
                    "type": ["string", "null"],
                    "description": "End date YYYY-MM-DD or null",
                },
            },
            "required": ["instrument_id", "start_date", "end_date"],
            "additionalProperties": False,
        },
        "strict": True,
    }


def validate_tool_args(name: str, raw_args: dict[str, Any]) -> SummarizeDatasetArgs:
    """Validate raw tool args; raises AppError on any violation."""
    from app.errors import AppError

    if name not in ALLOWED_TOOL_NAMES:
        raise AppError(400, "tool_not_allowed", "허용되지 않은 도구입니다: " + str(name))
    if not isinstance(raw_args, dict):
        raise AppError(422, "tool_args_invalid", "도구 인수는 객체여야 합니다.")
    rejected = _owner_like_keys(raw_args)
    if rejected:
        raise AppError(
            422,
            "tool_args_invalid",
            "소유자 식별자를 도구에 전달할 수 없습니다: " + ", ".join(rejected),
        )
    try:
        return SummarizeDatasetArgs.model_validate(raw_args)
    except Exception as exc:
        raise AppError(422, "tool_args_invalid", "도구 인수가 올바르지 않습니다: " + str(exc))


def execute_tool(
    db: Any, *, owner_uid: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Execute an allowlisted tool via the shared DataService."""
    from app.errors import AppError

    args = validate_tool_args(name, arguments)
    try:
        from app.services.data import DataService
    except ImportError as exc:
        raise AppError(503, "service_unavailable", "데이터 서비스를 사용할 수 없습니다.") from exc
    service = DataService(db)
    try:
        result = service.summary(
            owner_uid,
            args.instrument_id,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(502, "tool_failed", "summarize_dataset 실패: " + type(exc).__name__) from exc
    if not isinstance(result, dict):
        raise AppError(502, "tool_failed", "summarize_dataset 결과가 객체가 아닙니다.")
    return result
