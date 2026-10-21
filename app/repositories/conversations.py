"""Firestore repository for conversations, messages, and chat turns.

Conventions follow app.repositories.data: google-cloud-firestore
transactions via firestore.transactional, FieldFilter queries, opaque
cursors via app.pagination, and 404 (not 403) for foreign or missing
documents so ownership cannot be probed.

Documents:
- conversations/{id}: owner_uid, title, created_at, updated_at,
  last_message_preview, message_count, active_turn_id, revision,
  deleting, idempotency_key, create_fingerprint
- conversations/{id}/messages/{id}: role, content, created_at, sequence,
  turn_id, sources, tool_trace_summary
- conversations/{id}/turns/{request_id}: status running/completed/failed,
  attempt_token, lease_until, request_hash, message, instrument_id,
  started_at, completed_at, error_code, response, context_reference

Required composite indexes (firestore.indexes.json):
- conversations: owner_uid ASC + updated_at DESC + __name__ DESC
- messages: sequence ASC + __name__ ASC
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4, uuid5

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.errors import AppError
from app.pagination import decode_cursor, encode_cursor, scope_key

CONVERSATIONS_COLLECTION = "conversations"
MESSAGES_SUBCOLLECTION = "messages"
TURNS_SUBCOLLECTION = "turns"

LIST_SCOPE = "conversations:list:v1"
MESSAGE_SCOPE = "conversations:messages:v1"

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
DEFAULT_MESSAGE_LIMIT = 50
MAX_MESSAGE_LIMIT = 200

SWEEP_MESSAGE_BATCH = 400
SWEEP_TURN_BATCH = 100

CHAT_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

PREVIEW_CHARS = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return ""


def _parse_dt(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            raise AppError(422, "invalid_cursor", "cursor를 확인하세요.") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    raise AppError(422, "invalid_cursor", "cursor를 확인하세요.")


def conversation_id_for_chat(owner_uid: str, client_request_id: str) -> str:
    return str(uuid5(CHAT_NAMESPACE, owner_uid + ":" + str(client_request_id)))


def request_hash(message: str, instrument_id: str) -> str:
    joined = message + "\n" + instrument_id
    return hashlib.sha256(joined.encode()).hexdigest()


def message_preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    single = " ".join(str(text).split())
    if len(single) <= limit:
        return single
    return single[:limit] + "..."


def _require_uuid(raw: Any, field_name: str) -> str:
    try:
        return str(UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        raise AppError(422, "invalid_request", field_name + " 형식이 올바르지 않습니다.") from None


class ConversationRepository:
    def __init__(self, db: Any):
        self.db = db
        self.collection = db.collection(CONVERSATIONS_COLLECTION)

    @staticmethod
    def public_meta(snapshot: Any) -> dict[str, Any]:
        data = snapshot.to_dict() or {}
        return {
            "id": snapshot.id,
            "title": data.get("title", ""),
            "message_count": int(data.get("message_count", 0) or 0),
            "last_message_preview": data.get("last_message_preview", ""),
            "created_at": _iso(data.get("created_at")),
            "updated_at": _iso(data.get("updated_at")),
            "revision": int(data.get("revision", 1) or 1),
        }

    @staticmethod
    def public_message(snapshot: Any) -> dict[str, Any]:
        data = snapshot.to_dict() or {}
        return {
            "id": snapshot.id,
            "role": data.get("role", "user"),
            "content": data.get("content", ""),
            "sequence": int(data.get("sequence", 0) or 0),
            "created_at": _iso(data.get("created_at")),
            "turn_id": data.get("turn_id"),
            "sources": data.get("sources") or [],
            "tool_trace": data.get("tool_trace") or [],
            "data_as_of": data.get("data_as_of", ""),
        }

    def _parent_ref(self, conversation_id: str) -> Any:
        return self.collection.document(conversation_id)

    def _owned_parent(self, transaction: Any, ref: Any) -> tuple[Any, dict[str, Any]]:
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
        data = snapshot.to_dict() or {}
        return snapshot, data

    def _check_owner(self, data: dict[str, Any], owner_uid: str) -> None:
        if data.get("owner_uid") != owner_uid or data.get("deleting"):
            raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")

    @staticmethod
    def _lease_valid(turn: dict[str, Any], now: datetime | None = None) -> bool:
        now = now or _now()
        lease_until = turn.get("lease_until")
        if isinstance(lease_until, datetime):
            return lease_until > now
        return False

    def create(
        self,
        owner_uid: str,
        title: str,
        messages: list[dict[str, Any]],
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        doc_id = _require_uuid(conversation_id or uuid4(), "conversation_id")
        clean = [{"role": m.get("role"), "content": m.get("content")} for m in messages]
        fingerprint = hashlib.sha256(
            json.dumps(
                {"title": title, "messages": clean}, sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest()
        ref = self._parent_ref(doc_id)

        @firestore.transactional
        def write(transaction: Any) -> tuple[dict[str, Any], bool]:
            snapshot = ref.get(transaction=transaction)
            if snapshot.exists:
                current = snapshot.to_dict() or {}
                if current.get("owner_uid") != owner_uid or current.get("deleting"):
                    raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
                if (
                    idempotency_key
                    and current.get("idempotency_key") == idempotency_key
                    and current.get("create_fingerprint") == fingerprint
                ):
                    return self.public_meta(snapshot), False
                raise AppError(
                    409, "idempotency_conflict", "같은 키로 다른 요청을 보낼 수 없습니다."
                )
            deleted_job = (
                self.db.collection("job_runs").document(doc_id).get(transaction=transaction)
            )
            if deleted_job.exists:
                raise AppError(
                    409, "conversation_deleted", "삭제한 대화의 요청 ID는 다시 사용할 수 없습니다."
                )
            now = _now()
            parent = {
                "owner_uid": owner_uid,
                "title": title,
                "created_at": now,
                "updated_at": now,
                "last_message_preview": message_preview(clean[-1]["content"]) if clean else "",
                "message_count": len(clean),
                "active_turn_id": None,
                "revision": 1,
                "deleting": False,
                "idempotency_key": idempotency_key,
                "create_fingerprint": fingerprint,
            }
            transaction.create(ref, parent)
            for index, item in enumerate(clean, start=1):
                transaction.create(
                    ref.collection(MESSAGES_SUBCOLLECTION).document(uuid4().hex),
                    {
                        "role": item["role"],
                        "content": item["content"],
                        "created_at": now,
                        "sequence": index,
                        "turn_id": "seed",
                        "sources": [],
                        "tool_trace_summary": "",
                    },
                )
            exposed = {k: v for k, v in parent.items() if k != "owner_uid"}
            exposed["created_at"] = _iso(now)
            exposed["updated_at"] = _iso(now)
            return {"id": doc_id, **exposed}, True

        return write(self.db.transaction())

    def list(
        self, owner_uid: str, limit: int = DEFAULT_LIST_LIMIT, cursor: str | None = None
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
        scope = scope_key(LIST_SCOPE, owner_uid)
        query = (
            self.collection.where(filter=FieldFilter("owner_uid", "==", owner_uid))
            .where(filter=FieldFilter("deleting", "==", False))
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
            .order_by("__name__", direction=firestore.Query.DESCENDING)
        )
        if cursor:
            position = decode_cursor(cursor, scope)
            try:
                doc_id = position["id"]
                when = _parse_dt(position["u"])
                if not isinstance(doc_id, str) or not doc_id or "/" in doc_id:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise AppError(422, "invalid_cursor", "cursor를 확인하세요.") from exc
            query = query.start_after(
                {"updated_at": when, "__name__": self.collection.document(doc_id)}
            )
        raw = list(query.limit(limit + 1).stream())
        page = [self.public_meta(s) for s in raw[:limit]]
        next_cursor = None
        if len(raw) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(scope, {"id": last["id"], "u": last["updated_at"]})
        return {"items": page, "next_cursor": next_cursor}

    def get(self, owner_uid: str, conversation_id: str) -> dict[str, Any]:
        doc_id = _require_uuid(conversation_id, "conversation_id")
        snapshot = self._parent_ref(doc_id).get()
        if not snapshot.exists:
            raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
        data = snapshot.to_dict() or {}
        self._check_owner(data, owner_uid)
        return self.public_meta(snapshot)

    def messages(
        self,
        owner_uid: str,
        conversation_id: str,
        limit: int = DEFAULT_MESSAGE_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        doc_id = _require_uuid(conversation_id, "conversation_id")
        parent = self._parent_ref(doc_id).get()
        if not parent.exists:
            raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
        self._check_owner(parent.to_dict() or {}, owner_uid)
        limit = max(1, min(int(limit or DEFAULT_MESSAGE_LIMIT), MAX_MESSAGE_LIMIT))
        scope = scope_key(MESSAGE_SCOPE, owner_uid, doc_id)
        query = (
            self._parent_ref(doc_id)
            .collection(MESSAGES_SUBCOLLECTION)
            .order_by("sequence")
            .order_by("__name__")
        )
        if cursor:
            position = decode_cursor(cursor, scope)
            try:
                after = int(position["s"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AppError(422, "invalid_cursor", "cursor를 확인하세요.") from exc
            query = query.where(filter=FieldFilter("sequence", ">", after))
        snapshots = list(query.limit(limit + 1).stream())
        items = [self.public_message(s) for s in snapshots[:limit]]
        next_cursor = None
        if len(snapshots) > limit and items:
            next_cursor = encode_cursor(scope, {"s": items[-1]["sequence"]})
        return {"messages": items, "next_cursor": next_cursor}

    def recent_messages(
        self, owner_uid: str, conversation_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        doc_id = _require_uuid(conversation_id, "conversation_id")
        parent = self._parent_ref(doc_id).get()
        if not parent.exists:
            raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
        self._check_owner(parent.to_dict() or {}, owner_uid)
        query = (
            self._parent_ref(doc_id)
            .collection(MESSAGES_SUBCOLLECTION)
            .order_by("sequence", direction=firestore.Query.DESCENDING)
            .limit(max(1, min(int(limit or 20), 100)))
        )
        items = [self.public_message(s) for s in query.stream()]
        items.reverse()
        return items

    def mark_deleting(self, owner_uid: str, conversation_id: str) -> dict[str, Any]:
        doc_id = _require_uuid(conversation_id, "conversation_id")
        ref = self._parent_ref(doc_id)

        @firestore.transactional
        def write(transaction: Any) -> dict[str, Any]:
            snapshot, data = self._owned_parent(transaction, ref)
            if data.get("owner_uid") != owner_uid:
                raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
            if data.get("deleting"):
                return {"id": doc_id, "deleting": True, "already": True}
            active = data.get("active_turn_id")
            if active:
                turn_snap = (
                    ref.collection(TURNS_SUBCOLLECTION)
                    .document(str(active))
                    .get(transaction=transaction)
                )
                if turn_snap.exists:
                    turn = turn_snap.to_dict() or {}
                    if turn.get("status") == "running" and self._lease_valid(turn):
                        raise AppError(
                            409, "turn_conflict", "진행 중인 답변이 있어 삭제할 수 없습니다."
                        )
            transaction.update(
                ref,
                {
                    "deleting": True,
                    "revision": int(data.get("revision", 1) or 1) + 1,
                    "updated_at": _now(),
                },
            )
            return {"id": doc_id, "deleting": True, "already": False}

        return write(self.db.transaction())

    def sweep(
        self,
        owner_uid: str,
        conversation_id: str,
        max_messages: int = SWEEP_MESSAGE_BATCH,
        max_turns: int = SWEEP_TURN_BATCH,
    ) -> dict[str, Any]:
        doc_id = _require_uuid(conversation_id, "conversation_id")
        ref = self._parent_ref(doc_id)
        parent = ref.get()
        if not parent.exists or (parent.to_dict() or {}).get("owner_uid") != owner_uid:
            raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
        deleted_messages = self._delete_limited(
            ref.collection(MESSAGES_SUBCOLLECTION), max_messages
        )
        deleted_turns = self._delete_limited(ref.collection(TURNS_SUBCOLLECTION), max_turns)
        remaining = deleted_messages["remaining"] + deleted_turns["remaining"]
        parent_deleted = False
        if remaining == 0:
            ref.delete()
            parent_deleted = True
        return {
            "deleted_messages": deleted_messages["deleted"],
            "deleted_turns": deleted_turns["deleted"],
            "remaining": remaining,
            "parent_deleted": parent_deleted,
        }

    def _delete_limited(self, collection_ref: Any, batch_size: int) -> dict[str, int]:
        size = max(1, int(batch_size))
        snapshots = list(collection_ref.limit(size + 1).stream())
        batch = self.db.batch()
        for snapshot in snapshots[:size]:
            batch.delete(snapshot.reference)
        batch.commit()
        return {"deleted": min(len(snapshots), size), "remaining": max(0, len(snapshots) - size)}

    def acquire_turn(
        self,
        owner_uid: str,
        conversation_id: str | None,
        client_request_id: str,
        message: str,
        instrument_id: str,
        lease_seconds: int,
        default_title: str,
    ) -> dict[str, Any]:
        turn_id = _require_uuid(client_request_id, "client_request_id")
        if conversation_id:
            doc_id = _require_uuid(conversation_id, "conversation_id")
        else:
            doc_id = conversation_id_for_chat(owner_uid, turn_id)
        parent_ref = self._parent_ref(doc_id)
        turn_ref = parent_ref.collection(TURNS_SUBCOLLECTION).document(turn_id)
        digest = request_hash(message, instrument_id)

        @firestore.transactional
        def write(transaction: Any) -> dict[str, Any]:
            now = _now()
            parent_snap = parent_ref.get(transaction=transaction)
            if parent_snap.exists:
                parent = parent_snap.to_dict() or {}
                if parent.get("owner_uid") != owner_uid:
                    raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
                if parent.get("deleting"):
                    raise AppError(
                        409, "conversation_deleting", "삭제 중인 대화에는 질문할 수 없습니다."
                    )
            else:
                if conversation_id:
                    raise AppError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
                deleted_job = (
                    self.db.collection("job_runs").document(doc_id).get(transaction=transaction)
                )
                if deleted_job.exists:
                    raise AppError(
                        409,
                        "conversation_deleted",
                        "삭제한 대화의 요청 ID는 다시 사용할 수 없습니다.",
                    )
                parent = {
                    "owner_uid": owner_uid,
                    "title": default_title,
                    "created_at": now,
                    "updated_at": now,
                    "last_message_preview": message_preview(message),
                    "message_count": 0,
                    "active_turn_id": None,
                    "revision": 1,
                    "deleting": False,
                    "idempotency_key": None,
                    "create_fingerprint": None,
                }
            turn_snap = turn_ref.get(transaction=transaction)
            turn = turn_snap.to_dict() or {}
            if turn_snap.exists:
                if turn.get("request_hash") != digest:
                    raise AppError(
                        409, "idempotency_conflict", "같은 요청 ID로 다른 질문을 보낼 수 없습니다."
                    )
                if turn.get("status") == "completed":
                    return {
                        "conversation_id": doc_id,
                        "turn_id": turn_id,
                        "attempt_token": turn.get("attempt_token", ""),
                        "user_sequence": int(turn.get("user_sequence", 0) or 0),
                        "replay": turn.get("response") or {},
                        "created_conversation": False,
                    }
                if turn.get("status") == "running" and self._lease_valid(turn, now):
                    raise AppError(
                        409, "turn_conflict", "이전 질문을 처리 중입니다. 잠시 후 다시 시도하세요."
                    )
            active = parent.get("active_turn_id")
            if active and str(active) != turn_id:
                other_snap = (
                    parent_ref.collection(TURNS_SUBCOLLECTION)
                    .document(str(active))
                    .get(transaction=transaction)
                )
                if other_snap.exists:
                    other = other_snap.to_dict() or {}
                    if other.get("status") == "running" and self._lease_valid(other, now):
                        raise AppError(
                            409,
                            "turn_conflict",
                            "이전 질문을 처리 중입니다. 잠시 후 다시 시도하세요.",
                        )
            attempt_token = uuid4().hex
            lease_until = datetime.fromtimestamp(
                now.timestamp() + max(60, int(lease_seconds or 300)), tz=timezone.utc
            )
            # Retry a failed/expired turn at its original place; do not append another user.
            user_sequence = turn.get("user_sequence") or int(parent.get("message_count", 0)) + 1
            transaction.set(
                turn_ref,
                {
                    "owner_uid": owner_uid,
                    "conversation_id": doc_id,
                    "status": "running",
                    "attempt_token": attempt_token,
                    "lease_until": lease_until,
                    "request_hash": digest,
                    "message": message,
                    "instrument_id": instrument_id,
                    "user_sequence": user_sequence,
                    "started_at": now,
                    "completed_at": None,
                    "error_code": None,
                    "response": None,
                    "context_reference": {"instrument_id": instrument_id},
                },
            )
            if not turn_snap.exists:
                transaction.create(
                    parent_ref.collection(MESSAGES_SUBCOLLECTION).document(turn_id + "_user"),
                    {
                        "role": "user",
                        "content": message,
                        "created_at": now,
                        "sequence": user_sequence,
                        "turn_id": turn_id,
                        "sources": [],
                        "tool_trace_summary": "",
                    },
                )
            parent_updates = {
                "message_count": max(int(parent.get("message_count", 0)), user_sequence),
                "last_message_preview": message_preview(message),
                "updated_at": now,
                "active_turn_id": turn_id,
                "revision": int(parent.get("revision", 1) or 1) + 1,
            }
            if parent_snap.exists:
                transaction.update(parent_ref, parent_updates)
            else:
                transaction.create(parent_ref, {**parent, **parent_updates})
            return {
                "conversation_id": doc_id,
                "turn_id": turn_id,
                "attempt_token": attempt_token,
                "user_sequence": user_sequence,
                "replay": None,
                "created_conversation": not parent_snap.exists,
            }

        return write(self.db.transaction())

    def complete_turn(
        self,
        owner_uid: str,
        conversation_id: str,
        turn_id: str,
        attempt_token: str,
        assistant_text: str,
        sources: list[dict[str, Any]],
        tool_trace: list[dict[str, Any]],
        data_as_of: str,
    ) -> dict[str, Any]:
        doc_id = _require_uuid(conversation_id, "conversation_id")
        parent_ref = self._parent_ref(doc_id)
        turn_ref = parent_ref.collection(TURNS_SUBCOLLECTION).document(
            _require_uuid(turn_id, "turn_id")
        )

        @firestore.transactional
        def write(transaction: Any) -> dict[str, Any]:
            now = _now()
            parent_snap, parent = self._owned_parent(transaction, parent_ref)
            self._check_owner(parent, owner_uid)
            turn_snap = turn_ref.get(transaction=transaction)
            if not turn_snap.exists:
                raise AppError(404, "turn_not_found", "처리 중인 질문을 찾을 수 없습니다.")
            turn = turn_snap.to_dict() or {}
            if turn.get("attempt_token") != attempt_token:
                raise AppError(409, "stale_attempt", "만료된 시도입니다. 최신 시도를 확인하세요.")
            if parent.get("active_turn_id") != turn_snap.id:
                raise AppError(
                    409, "stale_attempt", "다른 질문이 처리 중입니다. 최신 시도를 확인하세요."
                )
            if turn.get("status") != "running":
                raise AppError(409, "turn_conflict", "이미 처리된 질문입니다.")
            assistant_sequence = int(parent.get("message_count", 0) or 0) + 1
            response = {
                "text": assistant_text,
                "sources": sources,
                "tool_trace": tool_trace,
                "data_as_of": data_as_of,
            }
            trace_bits = []
            for entry in tool_trace:
                mark = "ok" if entry.get("ok") else "fail"
                trace_bits.append(str(entry.get("tool", "")) + ":" + mark)
            transaction.create(
                parent_ref.collection(MESSAGES_SUBCOLLECTION).document(turn_snap.id + "_assistant"),
                {
                    "role": "assistant",
                    "content": assistant_text,
                    "created_at": now,
                    "sequence": assistant_sequence,
                    "turn_id": turn_snap.id,
                    "sources": sources,
                    "tool_trace": tool_trace,
                    "data_as_of": data_as_of,
                    "tool_trace_summary": "; ".join(trace_bits)[:1000],
                },
            )
            updates: dict[str, Any] = {
                "message_count": assistant_sequence,
                "last_message_preview": message_preview(assistant_text),
                "updated_at": now,
                "revision": int(parent.get("revision", 1) or 1) + 1,
            }
            if parent.get("active_turn_id") == turn_snap.id:
                updates["active_turn_id"] = None
            transaction.update(parent_ref, updates)
            transaction.update(
                turn_ref,
                {
                    "status": "completed",
                    "completed_at": now,
                    "error_code": None,
                    "response": response,
                },
            )
            return {"response": response, "assistant_sequence": assistant_sequence}

        return write(self.db.transaction())

    def fail_turn(
        self,
        owner_uid: str,
        conversation_id: str,
        turn_id: str,
        attempt_token: str,
        error_code: str,
    ) -> bool:
        doc_id = _require_uuid(conversation_id, "conversation_id")
        parent_ref = self._parent_ref(doc_id)
        turn_ref = parent_ref.collection(TURNS_SUBCOLLECTION).document(
            _require_uuid(turn_id, "turn_id")
        )

        @firestore.transactional
        def write(transaction: Any) -> bool:
            parent_snap = parent_ref.get(transaction=transaction)
            if not parent_snap.exists:
                return False
            parent = parent_snap.to_dict() or {}
            if parent.get("owner_uid") != owner_uid:
                return False
            turn_snap = turn_ref.get(transaction=transaction)
            if not turn_snap.exists:
                return False
            turn = turn_snap.to_dict() or {}
            if turn.get("attempt_token") != attempt_token or turn.get("status") != "running":
                return False
            now = _now()
            transaction.update(
                turn_ref, {"status": "failed", "completed_at": now, "error_code": error_code}
            )
            if parent.get("active_turn_id") == turn_snap.id:
                transaction.update(
                    parent_ref,
                    {
                        "active_turn_id": None,
                        "updated_at": now,
                        "revision": int(parent.get("revision", 1) or 1) + 1,
                    },
                )
            return True

        return write(self.db.transaction())
