import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.errors import AppError
from app.providers.openai_client import generate_reply
from app.tools.registry import summarize_dataset_tool_definition, validate_tool_args


def settings(**overrides):
    return SimpleNamespace(
        **{
            "openai_api_key": "test-only",
            "openai_model": "test-only",
            "openai_max_output_tokens": 1200,
            "max_tool_calls": 5,
            "max_tool_rounds": 3,
            **overrides,
        }
    )


class ResponsesStub:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.outputs)


def test_strict_schema_requires_nullable_dates_and_rejects_owner_arguments():
    definition = summarize_dataset_tool_definition()
    parameters = definition["parameters"]
    assert definition["strict"]
    assert set(parameters["required"]) == set(parameters["properties"])
    assert "null" in parameters["properties"]["start_date"]["type"]
    args = validate_tool_args(
        "summarize_dataset",
        {"instrument_id": "NASDAQ_AAPL", "start_date": "2025-10-21", "end_date": None},
    )
    assert args.start_date == date(2025, 10, 21)
    with pytest.raises(AppError):
        validate_tool_args(
            "summarize_dataset", {"instrument_id": "NASDAQ_AAPL", "owner_uid": "foreign"}
        )
    with pytest.raises(AppError):
        validate_tool_args("arbitrary_python", {})


def test_responses_tool_results_are_returned_and_output_budget_is_shared():
    stub = ResponsesStub(
        [
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call1",
                        "name": "summarize_dataset",
                        "arguments": json.dumps({"instrument_id": "NASDAQ_AAPL"}),
                    }
                ],
                "usage": {"output_tokens": 25},
            },
            {
                "output": [],
                "output_text": "실제 저장된 데이터 요약입니다.",
                "usage": {"output_tokens": 20},
            },
        ]
    )
    seen = []
    result = generate_reply(
        settings=settings(),
        instructions="server context",
        history=[],
        user_message="요약해줘",
        call_tool=lambda name, args: seen.append((name, args)) or {"count": 100},
        client=SimpleNamespace(responses=stub),
    )
    assert result["text"] == "실제 저장된 데이터 요약입니다."
    assert seen == [("summarize_dataset", {"instrument_id": "NASDAQ_AAPL"})]
    assert stub.calls[1]["max_output_tokens"] == 1175
    assert stub.calls[0]["store"] is False
    assert any(item.get("type") == "function_call_output" for item in stub.calls[1]["input"])


def test_last_round_forces_text_and_unfinished_calls_are_not_saved_as_answers():
    stub = ResponsesStub(
        [
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call1",
                        "name": "summarize_dataset",
                        "arguments": "{}",
                    }
                ],
                "output_text": "중간 내용",
                "usage": {"output_tokens": 10},
            },
            {"output": [], "output_text": "최종 답변", "usage": {"output_tokens": 20}},
        ]
    )
    result = generate_reply(
        settings=settings(max_tool_rounds=2),
        instructions="context",
        history=[],
        user_message="질문",
        call_tool=lambda *_: {"count": 100},
        client=SimpleNamespace(responses=stub),
    )
    assert stub.calls[-1]["tool_choice"] == "none"
    assert result["text"] == "최종 답변"


def test_provider_failure_and_missing_configuration_are_explicit():
    with pytest.raises(AppError) as missing:
        generate_reply(
            settings=settings(openai_api_key=None),
            instructions="",
            history=[],
            user_message="",
            call_tool=lambda *_: {},
            client=object(),
        )
    assert missing.value.status_code == 503

    def fail(**kwargs):
        raise RuntimeError("sensitive-provider-detail")

    with pytest.raises(AppError) as failure:
        generate_reply(
            settings=settings(),
            instructions="",
            history=[],
            user_message="질문",
            call_tool=lambda *_: {},
            client=SimpleNamespace(responses=SimpleNamespace(create=fail)),
        )
    assert "sensitive-provider-detail" not in failure.value.message


@pytest.mark.parametrize(
    "usage,call_id,code",
    [
        (None, "call1", "model_usage_unavailable"),
        ({"output_tokens": 10}, None, "model_protocol_error"),
        ({"output_tokens": 1200}, "call1", "model_budget_exhausted"),
    ],
)
def test_malformed_or_budget_exhausted_provider_response_stops_followup(usage, call_id, code):
    stub = ResponsesStub(
        [
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": "summarize_dataset",
                        "arguments": "{}",
                    }
                ],
                "usage": usage,
            }
        ]
    )
    with pytest.raises(AppError) as result:
        generate_reply(
            settings=settings(),
            instructions="context",
            history=[],
            user_message="질문",
            call_tool=lambda *_: {},
            client=SimpleNamespace(responses=stub),
        )
    assert result.value.code == code
    assert len(stub.calls) == 1
