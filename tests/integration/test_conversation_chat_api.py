from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_uid, get_db
from app.errors import AppError
from app.main import app
from app.repositories.conversations import ConversationRepository, conversation_id_for_chat
from app.schemas.conversation import ChatRequest
from app.services.chat import handle_chat

pytestmark = pytest.mark.integration
SETTINGS = SimpleNamespace(
    openai_api_key="unit-test-only", openai_model="unit-test-model", chat_lease_seconds=300
)


@pytest.fixture
def client(firestore_db, owner_uid):
    app.dependency_overrides[get_db] = lambda: firestore_db
    app.dependency_overrides[get_current_uid] = lambda: owner_uid
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_conversation_create_reload_pages_owner_and_delete(client, firestore_db, owner_uid):
    result = client.post(
        "/api/conversations",
        json={
            "title": "API test",
            "messages": [
                {"role": "user", "content": "첫 질문"},
                {"role": "assistant", "content": "첫 답변"},
            ],
        },
    )
    assert result.status_code == 201, result.text
    doc_id = result.json()["id"]
    assert client.get("/api/conversations").json()["items"][0]["id"] == doc_id
    page = client.get(f"/api/conversations/{doc_id}", params={"limit": 1}).json()
    assert page["messages"][0]["content"] == "첫 질문"
    second = client.get(
        f"/api/conversations/{doc_id}", params={"limit": 1, "cursor": page["next_cursor"]}
    )
    assert second.json()["messages"][0]["content"] == "첫 답변"
    app.dependency_overrides[get_current_uid] = lambda: "foreign-user"
    assert client.get(f"/api/conversations/{doc_id}").status_code == 404
    assert client.delete(f"/api/conversations/{doc_id}").status_code == 404
    app.dependency_overrides[get_current_uid] = lambda: owner_uid
    assert client.delete(f"/api/conversations/{doc_id}").status_code == 204
    parent = firestore_db.collection("conversations").document(doc_id)
    assert not parent.get().exists
    assert list(parent.collection("messages").stream()) == []
    assert list(parent.collection("turns").stream()) == []


def test_chat_saves_context_and_replays_without_second_model_call(client, firestore_db, owner_uid):
    payload = {"message": "저장한 데이터의 추세를 설명해줘", "client_request_id": str(uuid4())}
    seen = []

    def model(**kwargs):
        seen.append(kwargs)
        return {"text": "현재 저장된 분석 데이터가 없습니다.", "tool_trace": []}

    with (
        patch("app.api.chat.get_settings", return_value=SETTINGS),
        patch("app.services.chat.default_model_caller", return_value=model),
    ):
        first = client.post("/api/chat", json=payload)
        assert first.status_code == 200, first.text
        second = client.post("/api/chat", json=payload)
        assert second.status_code == 200, second.text
        assert first.json() == second.json()
        assert first.json()["data_as_of"] == ""
        assert len(seen) == 1
        assert '"count": 0' in seen[0]["instructions"]
        assert client.post("/api/chat", json={**payload, "message": "다른 질문"}).status_code == 409
    doc_id = first.json()["conversation_id"]
    assert doc_id == conversation_id_for_chat(owner_uid, payload["client_request_id"])
    detail = client.get(f"/api/conversations/{doc_id}").json()
    assert [row["role"] for row in detail["messages"]] == ["user", "assistant"]
    assert detail["message_count"] == 2
    assert client.delete(f"/api/conversations/{doc_id}").status_code == 204
    with patch("app.api.chat.get_settings", return_value=SETTINGS):
        retry_deleted = client.post("/api/chat", json=payload)
    assert retry_deleted.status_code == 409
    assert retry_deleted.json()["code"] == "conversation_deleted"


def test_model_failure_retry_retains_one_user_message(firestore_db, owner_uid):
    payload = ChatRequest(message="질문", client_request_id=uuid4())

    def unavailable(**kwargs):
        raise AppError(502, "model_error", "Controlled upstream failure")

    with pytest.raises(AppError):
        handle_chat(
            firestore_db,
            owner_uid=owner_uid,
            payload=payload,
            settings=SETTINGS,
            model_caller=unavailable,
        )
    repo = ConversationRepository(firestore_db)
    conv_id = conversation_id_for_chat(owner_uid, str(payload.client_request_id))
    assert repo.get(owner_uid, conv_id)["message_count"] == 1
    assert len(repo.messages(owner_uid, conv_id)["messages"]) == 1
    handle_chat(
        firestore_db,
        owner_uid=owner_uid,
        payload=payload,
        settings=SETTINGS,
        model_caller=lambda **kwargs: {"text": "다시 시도해 성공했습니다."},
    )
    messages = repo.messages(owner_uid, conv_id)["messages"]
    assert len(messages) == 2
    assert [row["sequence"] for row in messages] == [1, 2]


def test_expired_attempt_cannot_overwrite_new_turn(firestore_db, owner_uid):
    repo = ConversationRepository(firestore_db)
    first = repo.acquire_turn(owner_uid, None, str(uuid4()), "첫 질문", "INDEX_SP500", 300, "제목")
    parent = firestore_db.collection("conversations").document(first["conversation_id"])
    with pytest.raises(AppError) as conflict:
        repo.acquire_turn(
            owner_uid, first["conversation_id"], str(uuid4()), "새 질문", "INDEX_SP500", 300, "제목"
        )
    assert conflict.value.status_code == 409
    parent.collection("turns").document(first["turn_id"]).update(
        {"lease_until": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )
    newer = repo.acquire_turn(
        owner_uid, first["conversation_id"], str(uuid4()), "새 질문", "INDEX_SP500", 300, "제목"
    )
    with pytest.raises(AppError) as stale:
        repo.complete_turn(
            owner_uid,
            first["conversation_id"],
            first["turn_id"],
            first["attempt_token"],
            "오래된 응답",
            [],
            [],
            "",
        )
    assert stale.value.status_code == 409
    repo.complete_turn(
        owner_uid,
        newer["conversation_id"],
        newer["turn_id"],
        newer["attempt_token"],
        "새 답변",
        [],
        [],
        "",
    )


def test_large_deletion_is_resumable_and_has_owned_job_status(client, monkeypatch):
    created = client.post("/api/conversations", json={"title": "cleanup test"}).json()
    doc_id = created["id"]
    monkeypatch.setattr("app.api.conversations.SWEEP_ITERATIONS", 0)
    pending = client.delete(f"/api/conversations/{doc_id}")
    assert pending.status_code == 202, pending.text
    status_url = pending.json()["status_url"]
    assert client.get(status_url).json()["status"] == "pending"
    assert client.get("/api/conversations").json()["items"] == []
    monkeypatch.setattr("app.api.conversations.SWEEP_ITERATIONS", 10)
    assert client.delete(f"/api/conversations/{doc_id}").status_code == 204
    assert client.get(status_url).json()["status"] == "completed"
    app.dependency_overrides[get_current_uid] = lambda: "foreign-user"
    assert client.get(status_url).status_code == 404
