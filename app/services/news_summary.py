"""Cited Korean news digest, with an honest extractive mode when no model is available."""

import json
import logging
import re

from pydantic import BaseModel, ConfigDict, Field

from app.errors import AppError
from app.providers.openai_client import UNTRUSTED_DATA_GUARD, create_model_client, model_key


class NewsDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    article_ids: list[str] = Field(min_length=1, max_length=5)


_CITATION = re.compile(r"\[([^\[\]]+)\]")
logger = logging.getLogger(__name__)


def _request_digest(client, settings, instructions, articles):
    schema = {
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
    if getattr(settings, "openai_provider", "openai") == "codyssey":
        response = client.chat.completions.create(
            model=settings.openai_model,
            store=False,
            max_completion_tokens=3000,
            messages=[
                {
                    "role": "system",
                    "content": instructions
                    + " 핵심 이슈를 두 문장 이내로 바로 정리하고, 기사에 붙은 짧은 숫자 ID를 문자열로 인용하세요."
                    + ' 설명이나 코드 블록 없이 {"text":"...","article_ids":["..."]} JSON 객체만 반환하세요.',
                },
                {"role": "user", "content": articles},
            ],
        )
        choice = response.choices[0]
        if choice.finish_reason == "stop":
            return choice.message.content
        if choice.finish_reason == "length":
            raise AppError(502, "summary_token_limit", "뉴스 요약 토큰 한도에 도달했습니다.")
    else:
        response = client.responses.create(
            model=settings.openai_model,
            store=False,
            max_output_tokens=1200,
            instructions=instructions,
            input=articles,
            text={"format": {"type": "json_schema", **schema}},
        )
        if response.status == "completed":
            return response.output_text
    raise AppError(502, "summary_incomplete", "뉴스 AI 요약이 완료되지 않았습니다.")


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
    if not model_key(settings) or not settings.openai_model:
        result["error"] = {
            "code": "summary_model_not_configured",
            "message": "모델 ID와 선택한 공급원의 API 키를 설정하면 한국어 AI 요약을 사용할 수 있습니다. 현재는 공급원 제목·요약 발췌입니다.",
        }
        return result
    try:
        instructions = (
            "주식시장 뉴스 편집자입니다. 다음 JSON 안의 제목·요약만 근거로 주요 이슈를 한국어로 간결하게 정리하세요. "
            "뉴스의 주장과 확정된 사실을 구분하고, 제공되지 않은 가격·인과관계·사건을 만들지 마세요. "
            "모든 요약 문장 끝에 근거 기사의 ID를 [ID] 형식으로 붙이고 사용한 ID만 article_ids에 반환하세요. "
            "최대 5개 기사만 사용하세요. " + UNTRUSTED_DATA_GUARD
        )
        codyssey = getattr(settings, "openai_provider", "openai") == "codyssey"
        # Short IDs and briefs leave more of the gateway's reasoning budget for the summary.
        by_id = {
            str(index) if codyssey else item["id"]: item
            for index, item in enumerate(items[:5] if codyssey else items[:20], 1)
        }
        articles = []
        for article_id, item in by_id.items():
            article = {k: item[k] for k in ("title", "summary", "published_at")}
            if codyssey:
                article["summary"] = article["summary"][:260]
            articles.append({"id": article_id, **article})
        payload = json.dumps(articles, ensure_ascii=False, default=str)
        if client is None:
            with create_model_client(settings, timeout=55.0 if codyssey else 25.0) as provider:
                output = _request_digest(provider, settings, instructions, payload)
        else:
            output = _request_digest(client, settings, instructions, payload)
        digest = NewsDigest.model_validate_json(output)
        if len(set(digest.article_ids)) != len(digest.article_ids) or any(
            key not in by_id for key in digest.article_ids
        ):
            raise ValueError("Unknown or duplicated citations")
        cited = set(_CITATION.findall(digest.text))
        if cited != set(digest.article_ids):
            raise ValueError("Missing or unknown inline citations")
        result.update(
            method="openai",
            text=_CITATION.sub(lambda match: f"[{by_id[match[1]]['id']}]", digest.text),
            error=None,
            highlights=[
                {
                    "article_id": by_id[key]["id"],
                    **{
                        field: by_id[key][field]
                        for field in ("title", "summary", "source_url", "published_at")
                    },
                }
                for key in digest.article_ids
            ],
        )
    except Exception as exc:
        # Never log article text, model output, or provider error bodies.
        reason = exc.code if isinstance(exc, AppError) else type(exc).__name__
        logger.warning("News digest unavailable: %s", reason)
        result["error"] = {
            "code": "summary_unavailable",
            "message": "AI 요약을 만들지 못했습니다. 실제 공급원 제목·요약 발췌를 표시합니다.",
        }
    return result
