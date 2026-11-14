"""Provider/model payloads below are test doubles, never runtime market data."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.errors import AppError
from app.providers.openai_client import bounded_json, generate_reply
from app.services.news_summary import summarize_news
from app.tools.registry import tool_definitions, validate_tool_args

ITEMS = [
    {
        "id": "test-article",
        "title": "Test headline",
        "summary": "Test provider abstract",
        "source_url": "https://example.test/story",
        "published_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
]


def test_extractive_has_actual_citations_and_explains_missing_model():
    out = summarize_news(ITEMS, Settings(_env_file=None))
    assert out["method"] == "extractive"
    assert out["error"]["code"] == "summary_model_not_configured"
    assert out["highlights"][0]["source_url"] == ITEMS[0]["source_url"]
    assert summarize_news([], Settings(_env_file=None))["highlights"] == []


@pytest.mark.parametrize(
    "text,ids,valid",
    [
        ("실험 뉴스입니다. [test-article]", ["test-article"], True),
        ("근거가 없습니다.", ["test-article"], False),
        ("알 수 없는 출처입니다. [invented]", ["invented"], False),
        ("혼합 출처입니다. [test-article] [invented]", ["test-article"], False),
        ("중복입니다. [test-article]", ["test-article", "test-article"], False),
    ],
)
def test_strict_digest_checks_inline_and_known_article_ids(text, ids, valid):
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            status="completed", output_text=json.dumps({"text": text, "article_ids": ids})
        )

    out = summarize_news(
        ITEMS,
        Settings(_env_file=None, openai_api_key="test-only", openai_model="test-only"),
        client=SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    assert (out["method"] == "openai") is valid
    assert seen[0]["store"] is False
    assert seen[0]["text"]["format"]["strict"] is True
    assert "untrusted data" in seen[0]["instructions"]
    assert (out["error"] is None) is valid


def test_model_failure_never_discards_actual_articles_or_exposes_details():
    def fail(**_):
        raise RuntimeError("secret-test-detail")

    out = summarize_news(
        ITEMS,
        Settings(_env_file=None, openai_api_key="test-only", openai_model="test-only"),
        client=SimpleNamespace(responses=SimpleNamespace(create=fail)),
    )
    assert out["method"] == "extractive"
    assert out["highlights"][0]["title"] == ITEMS[0]["title"]
    assert "secret-test-detail" not in json.dumps(out, default=str)


def test_codyssey_digest_validates_plain_json_and_inline_citations():
    def create(**kwargs):
        assert "response_format" not in kwargs
        assert kwargs["store"] is False
        assert "JSON 객체" in kwargs["messages"][0]["content"]
        content = '{"text":"요약입니다. [test-article]","article_ids":["test-article"]}'
        return SimpleNamespace(
            choices=[
                SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))
            ]
        )

    result = summarize_news(
        ITEMS,
        Settings(
            _env_file=None,
            openai_provider="codyssey",
            codyssey_api_key="test-only",
            openai_model="test-only",
        ),
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    assert result["method"] == "openai"
    assert result["highlights"][0]["source_url"] == ITEMS[0]["source_url"]


def test_all_four_tools_are_strict_and_reject_owner_or_unknown_fields():
    definitions = tool_definitions()
    assert {t["name"] for t in definitions} == {
        "summarize_dataset",
        "search_news",
        "get_us_sentiment",
        "get_market_analysis",
    }
    for tool in definitions:
        assert tool["strict"]
        params = tool["parameters"]
        assert set(params["required"]) == set(params["properties"])
        assert params["additionalProperties"] is False
        with pytest.raises(AppError):
            validate_tool_args(tool["name"], {"owner_uid": "forbidden"})
    with pytest.raises(AppError):
        validate_tool_args("search_news", {"market": "crypto"})


def test_large_model_context_remains_valid_json_and_preserves_status_and_url():
    payload = {
        "items": [{**ITEMS[0], "summary": "long text " * 9000}] * 20,
        "quality_status": "stale",
        "error": {"code": "test_failure"},
    }
    rendered = bounded_json(payload, 8000)
    assert len(rendered) <= 8000
    parsed = json.loads(rendered)
    assert parsed["truncated"] is True
    assert parsed["data"]["quality_status"] == "stale"
    assert parsed["data"]["items"][0]["source_url"] == ITEMS[0]["source_url"]


def test_research_tool_sources_survive_model_roundtrip():
    responses = iter(
        [
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "news1",
                        "name": "search_news",
                        "arguments": '{"market":"US","instrument_id":null,"window":"24h"}',
                    }
                ],
                "usage": {"output_tokens": 10},
            },
            {"output": [], "output_text": "테스트 기사 요약", "usage": {"output_tokens": 10}},
        ]
    )
    result = generate_reply(
        settings=Settings(_env_file=None, openai_api_key="test-only", openai_model="test-only"),
        instructions="test",
        history=[],
        user_message="뉴스",
        call_tool=lambda *_: {
            "items": ITEMS,
            "summary": {"highlights": [{**ITEMS[0], "source_url": "https://example.test/digest"}]},
        },
        client=SimpleNamespace(responses=SimpleNamespace(create=lambda **_: next(responses))),
    )
    assert result["sources"][0]["url"] == ITEMS[0]["source_url"]
    assert any(source["url"] == "https://example.test/digest" for source in result["sources"])
    assert result["tool_trace"][0]["ok"]
