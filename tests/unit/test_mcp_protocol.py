# MCP protocol tests: handshake, strict schemas, validation, confirmation gating.
# Official protocol paths only: in-memory mcp.Client(server) plus one bounded
# Client(StdioServerParameters(...)) subprocess. No Firestore reads here and no live
# providers: doubles are explicit test-only stubs passed as service= to create_server.
# Synchronous tests; each drives the protocol with asyncio.run.
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import mcp.types as mcp_types
import pytest
from mcp import Client, StdioServerParameters
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.services.alerts import preview
from mcp_adapter.server import create_server, load_settings
from mcp_adapter.tools import MUTATIONS, CreateArgs

REPO_ROOT = Path(__file__).resolve().parents[2]
OWNER = "unit-owner"
PROJECT = "demo-unit-test"
OPEN_WORLD = {
    "get_quote",
    "get_price_history",
    "calculate_indicators",
    "search_news",
    "get_us_sentiment",
    "get_market_analysis",
    "get_market_overview",
}
EXPECTED_TOOL_NAMES = {
    "list_instruments",
    "get_quote",
    "get_price_history",
    "calculate_indicators",
    "summarize_dataset",
    "search_news",
    "get_us_sentiment",
    "get_market_analysis",
    "get_market_overview",
    "list_alerts",
    "preview_alert_rule",
    "create_alert",
    "update_alert",
    "reset_alert",
    "list_alert_events",
    "get_alert_event",
}
CANONICAL_RULE = {
    "instrument_id": "KRX_005930",
    "currency": "KRW",
    "steps": [{"op": "cross_above", "price": "35000"}],
    "session_scope": "regular",
    "repeat_mode": "once",
    "active": True,
    "idempotency_key": "unit-key-1",
}


def run(coro):
    return asyncio.run(coro)


def make_settings(**over):
    kw = {
        "_env_file": None,
        "firebase_project_id": PROJECT,
        "allowed_user_uids": OWNER,
        "mcp_owner_uid": OWNER,
    }
    kw.update(over)
    return Settings(**kw)


class StubService:
    def __init__(self):
        self.read_calls = []
        self.preview_calls = []
        self.mutate_calls = []
        self.read_result = {"ok": True}
        self.draft = {"action": "stub-preview"}
        self.mutate_result = {"mutated": True}
        self.read_error = None

    def read(self, name, args):
        self.read_calls.append((name, args))
        if self.read_error is not None:
            raise self.read_error
        return self.read_result

    def mutation_preview(self, name, args):
        self.preview_calls.append((name, args))
        return dict(self.draft)

    def mutate(self, name, args):
        self.mutate_calls.append((name, args))
        return dict(self.mutate_result)


def error_of(result):
    assert result.is_error, result.structured_content
    return result.structured_content, result.content[0].text


def test_settings_require_owner_project_and_bounded_timeout():
    with pytest.raises(AppError):
        create_server(None, make_settings(mcp_owner_uid=None))
    with pytest.raises(AppError):
        create_server(None, make_settings(mcp_owner_uid="intruder"))
    with pytest.raises(AppError):
        create_server(None, make_settings(firebase_project_id=None))
    with pytest.raises(ValidationError):
        make_settings(mcp_confirmation_timeout_seconds=10)
    with pytest.raises(ValidationError):
        make_settings(mcp_confirmation_timeout_seconds=400)
    assert make_settings(mcp_confirmation_timeout_seconds=30)
    assert make_settings(mcp_confirmation_timeout_seconds=300)


def test_load_settings_rejects_missing_or_relative_env_file(tmp_path):
    with pytest.raises(AppError):
        load_settings(str(tmp_path / "does-not-exist.env"))
    with pytest.raises(AppError):
        load_settings("relative/path.env")


def test_initialize_lists_sixteen_tools_with_strict_schemas_and_annotations():
    async def main():
        server = create_server(None, make_settings(), service=StubService())
        async with Client(server, mode="legacy") as client:
            assert client.server_info.name == "stock-market-assistant"
            listed = await client.list_tools()
            assert {t.name for t in listed.tools} == EXPECTED_TOOL_NAMES
            assert len(listed.tools) == 16
            for tool in listed.tools:
                schema = tool.input_schema
                assert schema["additionalProperties"] is False, tool.name
                ann = tool.annotations
                assert ann.read_only_hint == (tool.name not in MUTATIONS), tool.name
                assert ann.destructive_hint == (tool.name in {"update_alert", "reset_alert"})
                assert ann.idempotent_hint == (
                    tool.name not in MUTATIONS or tool.name == "create_alert"
                ), tool.name
                assert ann.open_world_hint == (tool.name in OPEN_WORLD), tool.name
            create = next(t for t in listed.tools if t.name == "create_alert")
            rule = create.input_schema["properties"]["rule"]
            nested = create.input_schema["$defs"][rule["$ref"].split("/")[-1]]
            assert nested["additionalProperties"] is False

    run(main())


def test_read_receives_exact_validated_values():
    stub = StubService()

    async def main():
        server = create_server(None, make_settings(), service=stub)
        async with Client(server, mode="legacy") as client:
            quoted = await client.call_tool("get_quote", {"instrument_id": "KRX_005930"})
            assert not quoted.is_error
            history = await client.call_tool(
                "get_price_history",
                {
                    "instrument_id": "KRX_005930",
                    "start_date": "2026-01-05",
                    "end_date": "2026-01-12",
                    "interval": "1d",
                },
            )
            assert not history.is_error

    run(main())
    assert [n for n, _ in stub.read_calls] == ["get_quote", "get_price_history"]
    assert type(stub.read_calls[0][1]).__name__ == "AnalysisArgs"
    assert stub.read_calls[0][1].instrument_id == "KRX_005930"
    assert stub.read_calls[1][1].start_date.isoformat() == "2026-01-05"
    assert stub.read_calls[1][1].end_date.isoformat() == "2026-01-12"


def test_strict_validation_rejects_extras_bad_prices_and_unknown_tools():
    async def main():
        server = create_server(None, make_settings(), service=StubService())
        async with Client(server, mode="legacy") as client:
            uid_extra = await client.call_tool(
                "list_alerts", {"active": True, "owner_uid": "intruder"}
            )
            code, _ = error_of(uid_extra)
            assert code["error"]["code"] == "tool_args_invalid"
            nested = await client.call_tool(
                "create_alert", {"rule": {**CANONICAL_RULE, "owner_uid": "intruder"}}
            )
            code, text = error_of(nested)
            assert code["error"]["code"] == "tool_args_invalid"
            assert "intruder" not in text
            step = await client.call_tool(
                "create_alert",
                {
                    "rule": {
                        **CANONICAL_RULE,
                        "steps": [{"op": "cross_above", "price": "35000", "x": 1}],
                    }
                },
            )
            code, _ = error_of(step)
            assert code["error"]["code"] == "tool_args_invalid"
            bad = await client.call_tool(
                "create_alert",
                {"rule": {**CANONICAL_RULE, "steps": [{"op": "gte", "price": "35000.5"}]}},
            )
            code, text = error_of(bad)
            assert code["error"]["code"] == "tool_args_invalid"
            assert "35000.5" not in text
            unknown = await client.call_tool("fly_to_the_moon", {})
            code, _ = error_of(unknown)
            assert code["error"]["code"] == "tool_not_allowed"

    run(main())


def test_provider_exceptions_are_sanitized_and_never_echo_secrets():
    secret = "PROVIDER_SECRET_marker-9f2"
    stub = StubService()
    stub.read_error = RuntimeError("boom " + secret)

    async def main():
        server = create_server(None, make_settings(), service=stub)
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("get_quote", {"instrument_id": "KRX_005930"})
            code, text = error_of(result)
            assert code["error"]["code"] == "tool_unavailable"
            assert secret not in text and secret not in json.dumps(code)
            stub.read_error = AppError(502, "provider_down", "leaked " + secret)
            app_err = await client.call_tool("get_quote", {"instrument_id": "KRX_005930"})
            code, text = error_of(app_err)
            assert code["error"]["code"] == "provider_down"
            assert secret not in text and secret not in json.dumps(code)

    run(main())


@pytest.mark.parametrize(
    "mode", ["decline", "cancel", "accept_false", "unsupported", "timeout", "accept"]
)
def test_confirmation_gating_never_mutates_without_human_accept(mode):
    stub = StubService()
    captured = []
    if mode == "unsupported":
        callback = None
    elif mode == "timeout":

        async def callback(ctx, params):
            await asyncio.sleep(0.1)
            return mcp_types.ElicitResult(action="accept", content={"confirmed": True})
    elif mode == "accept":

        async def callback(ctx, params):
            captured.append(params)
            return mcp_types.ElicitResult(action="accept", content={"confirmed": True})
    else:
        action = "accept" if mode == "accept_false" else mode
        content = {"confirmed": False} if mode == "accept_false" else None

        async def callback(ctx, params):
            captured.append(params)
            return mcp_types.ElicitResult(action=action, content=content)

    validated = CreateArgs.model_validate({"rule": CANONICAL_RULE})
    expected_draft = {
        "action": "create_alert",
        **preview(validated.rule.model_dump(exclude={"idempotency_key"})),
    }

    stub.draft = expected_draft

    async def main():
        config = make_settings()
        if mode == "timeout":
            # Accelerate only this test's server deadline; public settings retain
            # their validated 30..300-second range.
            config.mcp_confirmation_timeout_seconds = 0.01
        server = create_server(None, config, service=stub)
        kwargs = {"mode": "legacy"}
        if callback is not None:
            kwargs["elicitation_callback"] = callback
        async with Client(server, **kwargs) as client:
            return await client.call_tool("create_alert", {"rule": CANONICAL_RULE})

    result = run(main())
    if mode == "accept":
        assert not result.is_error, result.structured_content
        assert result.structured_content == {"mutated": True}
        assert len(stub.mutate_calls) == 1
        assert stub.mutate_calls[0][1].rule.idempotency_key == "unit-key-1"
        shown = json.loads(captured[0].message[captured[0].message.index("{") :])
        assert shown == expected_draft
        return
    code, _ = error_of(result)
    expected = {
        "decline": "confirmation_declined",
        "cancel": "confirmation_declined",
        "accept_false": "confirmation_declined",
        "unsupported": "confirmation_unavailable",
        "timeout": "confirmation_timeout",
    }
    assert code["error"]["code"] == expected[mode]
    assert stub.preview_calls and not stub.mutate_calls


def test_subprocess_catalog_and_fail_closed_startup(tmp_path):
    def write_env(name, **over):
        values = {
            "APP_ENV": "test",
            "FIREBASE_PROJECT_ID": "demo-stock-assistant",
            "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
            "ALLOWED_USER_UIDS": "demo-owner",
            "MCP_OWNER_UID": "demo-owner",
        }
        values.update(over)
        path = tmp_path / name
        with open(path, "w") as handle:
            for key, value in values.items():
                handle.write(key + "=" + value + chr(10))
        return path

    env_file = write_env("mcp-demo.env")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_adapter.server", "--env-file", str(env_file)],
        cwd="/private/tmp",
        env={},
    )

    async def catalog():
        async with Client(params, mode="legacy") as client:
            listed = await client.list_tools()
            assert len(listed.tools) == 16
            result = await client.call_tool("list_instruments", {"market": "KR"})
            assert not result.is_error
            items = result.structured_content["items"]
            assert items, "catalog must be served without any data read"
            assert {item["market"] for item in items} == {"KR"}
            assert all(item["id"] and item["currency"] for item in items)

    run(catalog())
    bad_env = write_env("mcp-bad-owner.env", MCP_OWNER_UID="not-allowed")

    async def fail_closed():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mcp_adapter.server",
            "--env-file",
            str(bad_env),
            cwd="/private/tmp",
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        finally:
            if proc.returncode is None:
                proc.kill()
        return proc.returncode, out, err

    code, out, err = run(fail_closed())
    assert code == 1
    assert out == b"", "no protocol bytes or data may precede the owner check"
    assert b"MCP startup failed" in err


def test_modern_read_connection_fails_closed_without_nested_confirmation_channel():
    stub = StubService()

    async def callback(ctx, params):
        return mcp_types.ElicitResult(action="accept", content={"confirmed": True})

    async def scenario():
        async with Client(
            create_server(None, make_settings(), service=stub), elicitation_callback=callback
        ) as client:
            assert not (await client.call_tool("list_instruments", {})).is_error
            response = await client.call_tool("create_alert", {"rule": CANONICAL_RULE})
            assert response.structured_content["error"]["code"] == "confirmation_unavailable"

    run(scenario())
    assert not stub.mutate_calls
