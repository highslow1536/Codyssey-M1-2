"""Allowlisted chat tool registry.

Dataset summaries and bounded research reads are exposed. Arguments are strictly
validated with Pydantic (extra fields forbidden); there is deliberately no
owner-UID field. The owner is always injected server-side.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_TOOL_NAMES = ("summarize_dataset", "search_news", "get_us_sentiment", "get_market_analysis")

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


class AnalysisArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: str = Field(min_length=1, max_length=64, pattern="^[A-Za-z0-9_.-]+$")


class NewsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: Literal["KR", "US"]
    instrument_id: str | None = Field(default=None, max_length=64)
    window: Literal["24h", "7d"] = "24h"


class SentimentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def tool_definitions():
    def define(name, description, properties):
        return {
            "type": "function",
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        }

    return [
        summarize_dataset_tool_definition(),
        define(
            "search_news",
            "Read recent real Naver/Finnhub news and a cited digest. Collect only if cache is due. Empty or unavailable results do not mean no market events.",
            {
                "market": {"type": "string", "enum": ["KR", "US"]},
                "instrument_id": {
                    "type": ["string", "null"],
                    "description": "Catalog ID or null for market-wide news",
                },
                "window": {"type": "string", "enum": ["24h", "7d"]},
            },
        ),
        define(
            "get_us_sentiment",
            "Read CNN US stock Fear & Greed and VIX. Preserve stale/unavailable status and observation times. This is not a Korean or crypto index.",
            {},
        ),
        define(
            "get_market_analysis",
            "Read original Yahoo adjusted-close returns, annualized realized volatility, Wilder RSI, moving averages and volume ratio; separate from editable user data.",
            {"instrument_id": {"type": "string"}},
        ),
    ]


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


def validate_tool_args(name: str, raw_args: dict[str, Any]) -> BaseModel:
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
        model = {
            "summarize_dataset": SummarizeDatasetArgs,
            "search_news": NewsArgs,
            "get_us_sentiment": SentimentArgs,
            "get_market_analysis": AnalysisArgs,
        }[name]
        return model.model_validate(raw_args)
    except Exception as exc:
        raise AppError(422, "tool_args_invalid", "도구 인수가 올바르지 않습니다: " + str(exc))


def execute_tool(
    db: Any, *, owner_uid: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Execute an allowlisted tool via the shared DataService."""
    from app.errors import AppError

    args = validate_tool_args(name, arguments)
    if name != "summarize_dataset":
        from app.services.research import ResearchService

        service = ResearchService(db)
        if name == "search_news":
            result = service.news(args.market, args.instrument_id, args.window)
            return {
                **result,
                "items": result["items"][:5],
                "returned_articles": min(5, len(result["items"])),
                "total_collected_articles": len(result["items"]),
            }
        if name == "get_market_analysis":
            return service.analysis(args.instrument_id)
        result = service.sentiment()
        return {
            "cnn": {key: value for key, value in result["cnn"].items() if key != "history"},
            "vix": result["vix"],
        }
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
