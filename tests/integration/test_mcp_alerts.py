"""Real Firestore + official MCP protocol; human responses are explicit test doubles."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from mcp import Client, types

from app.config import Settings
from app.repositories.alerts import AlertRepository
from mcp_adapter.server import create_server
from mcp_adapter.tools import ToolService

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 23, 1, 30, tzinfo=timezone.utc)
RULE = {
    "instrument_id": "KRX_005930",
    "currency": "KRW",
    "steps": [{"op": "cross_above", "price": "35000.00"}, {"op": "lte", "price": "30000"}],
    "idempotency_key": "protocol-create",
}


def server(db, uid):
    settings = Settings(
        _env_file=None,
        firebase_project_id="demo-stock-assistant",
        mcp_owner_uid=uid,
        allowed_user_uids=uid,
    )
    return create_server(db, settings, service=ToolService(db, settings, clock=lambda: NOW))


def update_values(row, **overrides):
    return {
        **{
            k: row[k]
            for k in (
                "instrument_id",
                "currency",
                "steps",
                "session_scope",
                "repeat_mode",
                "expires_at",
                "step_timeout_hours",
                "active",
            )
        },
        "expected_revision": row["revision"],
        "reset_state": False,
        **overrides,
    }


async def accept(ctx, params):
    return types.ElicitResult(action="accept", content={"confirmed": True})


def test_protocol_create_persists_exact_preview_idempotently_without_sending(
    firestore_db, owner_uid
):
    shown = []

    async def human(ctx, params):
        shown.append(json.loads(params.message[params.message.index("{") :]))
        return await accept(ctx, params)

    async def scenario():
        async with Client(
            server(firestore_db, owner_uid), mode="legacy", elicitation_callback=human
        ) as client:
            first = await client.call_tool("create_alert", {"rule": RULE})
            second = await client.call_tool("create_alert", {"rule": RULE})
            assert not first.is_error and not second.is_error
            assert first.structured_content["id"] == second.structured_content["id"]
            assert "owner_uid" not in first.structured_content
            return first.structured_content

    saved = asyncio.run(scenario())
    repo = AlertRepository(firestore_db)
    rows = repo.list_rules(owner_uid)
    assert len(rows) == 1
    assert rows[0]["id"] == saved["id"]
    assert (
        shown[0]["rule"]["steps"]
        == rows[0]["steps"]
        == [{"op": "cross_above", "price": "35000"}, {"op": "lte", "price": "30000"}]
    )
    assert "idempotency_key" not in shown[0]["rule"]
    assert repo.list_events(owner_uid)["items"] == []
    assert list(repo.ready(owner_uid, NOW, 30)) == []


def test_protocol_decline_has_no_persistent_rule(firestore_db, owner_uid):
    async def decline(ctx, params):
        return types.ElicitResult(action="decline")

    async def scenario():
        async with Client(
            server(firestore_db, owner_uid), mode="legacy", elicitation_callback=decline
        ) as client:
            response = await client.call_tool("create_alert", {"rule": RULE})
            assert response.structured_content["error"]["code"] == "confirmation_declined"

    asyncio.run(scenario())
    assert AlertRepository(firestore_db).list_rules(owner_uid) == []


def test_protocol_owner_fences_rules_events_and_mutations(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    from app.schemas.alert import AlertCreate

    owned = repo.create(owner_uid, AlertCreate.model_validate(RULE).model_dump(), NOW)
    event = firestore_db.collection("alert_events").document("mcp-" + owner_uid)
    event.set({"owner_uid": owner_uid, "generated_at": NOW, "parts": [], "status": "queued"})

    async def scenario():
        async with Client(
            server(firestore_db, owner_uid + "-other"), mode="legacy", elicitation_callback=accept
        ) as other:
            assert (await other.call_tool("list_alerts", {})).structured_content["items"] == []
            assert (await other.call_tool("list_alert_events", {})).structured_content[
                "items"
            ] == []
            for name, args in (
                (
                    "update_alert",
                    {"alert_id": owned["id"], "rule": update_values(owned, active=False)},
                ),
                ("reset_alert", {"alert_id": owned["id"], "expected_revision": 1}),
                ("get_alert_event", {"event_id": event.id}),
            ):
                response = await other.call_tool(name, args)
                assert response.is_error
                assert response.structured_content["error"]["code"] in {
                    "alert_not_found",
                    "message_not_found",
                }
        async with Client(server(firestore_db, owner_uid), mode="legacy") as owner:
            page = await owner.call_tool("list_alert_events", {"limit": 1})
            assert page.structured_content["items"][0]["id"] == event.id
            detail = await owner.call_tool("get_alert_event", {"event_id": event.id})
            assert not detail.is_error and detail.structured_content["id"] == event.id

    asyncio.run(scenario())
    assert repo.get_rule(owner_uid, owned["id"])["revision"] == 1


def test_protocol_update_and_reset_reuse_revision_and_state_rules(firestore_db, owner_uid):
    async def scenario():
        async with Client(
            server(firestore_db, owner_uid), mode="legacy", elicitation_callback=accept
        ) as client:
            row = (await client.call_tool("create_alert", {"rule": RULE})).structured_content
            paused = await client.call_tool(
                "update_alert", {"alert_id": row["id"], "rule": update_values(row, active=False)}
            )
            assert not paused.is_error
            assert paused.structured_content["active"] is False
            assert paused.structured_content["revision"] == 2
            reset = await client.call_tool(
                "reset_alert", {"alert_id": row["id"], "expected_revision": 2}
            )
            assert not reset.is_error
            assert reset.structured_content["active"] is True
            assert reset.structured_content["state"]["stage"] == 0
            assert reset.structured_content["revision"] == 3
            stale = await client.call_tool(
                "reset_alert", {"alert_id": row["id"], "expected_revision": 2}
            )
            assert stale.structured_content["error"]["code"] == "revision_conflict"

    asyncio.run(scenario())


def test_change_during_confirmation_cannot_overwrite_new_revision(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    from app.schemas.alert import AlertCreate

    row = repo.create(owner_uid, AlertCreate.model_validate(RULE).model_dump(), NOW)

    async def concurrent_edit(ctx, params):
        repo.update(owner_uid, row["id"], update_values(row, active=False), NOW)
        return await accept(ctx, params)

    async def scenario():
        async with Client(
            server(firestore_db, owner_uid), mode="legacy", elicitation_callback=concurrent_edit
        ) as client:
            response = await client.call_tool(
                "reset_alert", {"alert_id": row["id"], "expected_revision": 1}
            )
            assert response.is_error
            assert response.structured_content["error"]["code"] == "revision_conflict"

    asyncio.run(scenario())
    saved = repo.get_rule(owner_uid, row["id"])
    assert saved["revision"] == 2 and saved["active"] is False


def test_stdio_process_receives_human_confirmation_and_persists(firestore_db, owner_uid, tmp_path):
    import sys

    from mcp.client.stdio import StdioServerParameters

    env_file = tmp_path / "mcp.env"
    env_file.write_text(
        "APP_ENV=test\nFIREBASE_PROJECT_ID=demo-stock-assistant\n"
        "FIRESTORE_EMULATOR_HOST=127.0.0.1:8080\n"
        f"ALLOWED_USER_UIDS={owner_uid}\nMCP_OWNER_UID={owner_uid}\n"
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_adapter.server", "--env-file", str(env_file)],
        cwd="/private/tmp",
    )
    shown = []

    async def human(ctx, form):
        shown.append(form.message)
        return await accept(ctx, form)

    async def scenario():
        async with Client(params, mode="legacy", elicitation_callback=human) as client:
            created = await client.call_tool("create_alert", {"rule": RULE})
            assert not created.is_error
            listed = await client.call_tool("list_alerts", {})
            assert listed.structured_content["items"][0]["id"] == created.structured_content["id"]

    asyncio.run(scenario())
    assert len(shown) == 1 and "35000" in shown[0] and "30000" in shown[0]
    assert len(AlertRepository(firestore_db).list_rules(owner_uid)) == 1


def test_create_retry_after_expiry_returns_existing_rule(firestore_db, owner_uid):
    from datetime import timedelta

    current = [NOW]
    settings = Settings(
        _env_file=None,
        firebase_project_id="demo-stock-assistant",
        mcp_owner_uid=owner_uid,
        allowed_user_uids=owner_uid,
    )
    service = ToolService(firestore_db, settings, clock=lambda: current[0])
    expiring = {**RULE, "expires_at": (NOW + timedelta(minutes=1)).isoformat()}

    async def scenario():
        async with Client(
            create_server(firestore_db, settings, service=service),
            mode="legacy",
            elicitation_callback=accept,
        ) as client:
            first = await client.call_tool("create_alert", {"rule": expiring})
            assert not first.is_error
            current[0] = NOW + timedelta(minutes=2)
            retried = await client.call_tool("create_alert", {"rule": expiring})
            assert not retried.is_error
            assert first.structured_content["id"] == retried.structured_content["id"]
            new_expired = {**expiring, "idempotency_key": "a-different-create"}
            rejected = await client.call_tool("create_alert", {"rule": new_expired})
            assert rejected.structured_content["error"]["code"] == "invalid_expiry"

    asyncio.run(scenario())
    assert len(AlertRepository(firestore_db).list_rules(owner_uid)) == 1
