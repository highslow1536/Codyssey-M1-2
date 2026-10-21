"""Cited Korean news digest, with an honest extractive mode when no model is available."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from app.errors import AppError
from app.providers.openai_client import UNTRUSTED_DATA_GUARD


class NewsDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    article_ids: list[str] = Field(min_length=1, max_length=5)


def summarize_news(items, settings, *, use_model=True, client=None):
    highlights = [
        {
            "article_id": item["id"],
            **{k: item[k] for k in ("title", "summary", "source_url", "published_at")},
        }
        for item in items[:5]
    ]
    result = {
        "method": "extractive",
        "highlights": highlights,
        "text": f"공급원이 제공한 뉴스 {len(items)}건 중 최근 {len(highlights)}건의 제목·요약을 정리했습니다. 원문 전문은 수집하지 않습니다."
        if items
        else "이 조회에서 수집된 기간 내 뉴스가 없습니다. 공급원의 조회 범위에 한정된 결과입니다.",
        "content_basis": "provider_title_and_summary",
        "error": None,
    }
    if not items or not use_model:
        return result
    if not settings.openai_api_key or not settings.openai_model:
        result["error"] = {
            "code": "summary_model_not_configured",
            "message": "한국어 AI 요약은 OPENAI_API_KEY와 OPENAI_MODEL 설정 후 사용할 수 있습니다. 현재는 공급원 제목·요약 발췌입니다.",
        }
        return result
    try:
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key, timeout=25.0, max_retries=0)
        response = client.responses.create(
            model=settings.openai_model,
            store=False,
            max_output_tokens=1200,
            instructions=(
                "주식시장 뉴스 편집자입니다. 다음 JSON 안의 제목·요약만 근거로 주요 이슈를 한국어로 간결하게 정리하세요. "
                "뉴스의 주장과 확정된 사실을 구분하고, 제공되지 않은 가격·인과관계·사건을 만들지 마세요. "
                "모든 요약 문장 끝에 근거 기사의 ID를 [ID] 형식으로 붙이고 사용한 ID만 article_ids에 반환하세요. "
                "최대 5개 기사만 사용하세요. " + UNTRUSTED_DATA_GUARD
            ),
            input=json.dumps(
                [
                    {k: item[k] for k in ("id", "title", "summary", "published_at")}
                    for item in items[:20]
                ],
                ensure_ascii=False,
                default=str,
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "news_digest",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "article_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["text", "article_ids"],
                        "additionalProperties": False,
                    },
                }
            },
        )
        if response.status != "completed":
            raise AppError(502, "summary_incomplete", "뉴스 AI 요약이 완료되지 않았습니다.")
        digest = NewsDigest.model_validate_json(response.output_text)
        by_id = {item["id"]: item for item in items[:20]}
        if len(set(digest.article_ids)) != len(digest.article_ids) or any(
            key not in by_id for key in digest.article_ids
        ):
            raise ValueError("Unknown or duplicated citations")
        cited = set(re.findall(r"\[([^\[\]]+)\]", digest.text))
        if cited != set(digest.article_ids):
            raise ValueError("Missing or unknown inline citations")
        result.update(
            method="openai",
            text=digest.text,
            error=None,
            highlights=[
                {
                    "article_id": key,
                    **{
                        field: by_id[key][field]
                        for field in ("title", "summary", "source_url", "published_at")
                    },
                }
                for key in digest.article_ids
            ],
        )
    except Exception:
        result["error"] = {
            "code": "summary_unavailable",
            "message": "AI 요약을 만들지 못했습니다. 실제 공급원 제목·요약 발췌를 표시합니다.",
        }
    return result
