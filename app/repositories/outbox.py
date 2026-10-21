"""Reusable durable, fenced Discord outbox for briefings and alerts."""

import hashlib
from datetime import timedelta
from uuid import uuid4

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.errors import AppError


def public(snapshot):
    return {
        "id": snapshot.id,
        **{k: v for k, v in snapshot.to_dict().items() if not k.startswith("_")},
    }


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OutboxRepository:
    NOT_FOUND_CODE = "message_not_found"
    NOT_FOUND_MESSAGE = "메시지를 찾을 수 없습니다."
    NOT_SENDABLE_CODE = "message_not_sendable"
    NOT_SENDABLE_MESSAGE = "전송할 메시지를 먼저 생성하세요."

    def __init__(self, db, collection="briefings", outbox_collection="outbox"):
        self.db = db
        self.messages = db.collection(collection)
        self.outbox = db.collection(outbox_collection)
        self.destination = db.collection("delivery_destinations").document("default")

    def _owned(self, ref, uid, transaction=None):
        snapshot = ref.get(transaction=transaction)
        if (
            not snapshot.exists
            or snapshot.to_dict()["owner_uid"] != uid
            or snapshot.to_dict().get("_deleted")
        ):
            raise AppError(404, self.NOT_FOUND_CODE, self.NOT_FOUND_MESSAGE)
        return snapshot

    def get(self, uid, message_id):
        return public(self._owned(self.messages.document(message_id), uid))

    @staticmethod
    def _parts(contents):
        return [
            {
                "content": text,
                "status": "queued",
                "attempts": 0,
                "last_error": None,
                "message_id": None,
                "uncertain": False,
            }
            for text in contents
        ]

    def queue(self, uid, message_id, key, contents, now):
        ref = self.messages.document(message_id)
        out = self.outbox.document(message_id)

        @firestore.transactional
        def write(tx):
            row = self._owned(ref, uid, tx)
            old = row.to_dict()
            out.get(transaction=tx)
            if digest(key) in old.get("_send_keys", []):
                return public(row)
            state = old["delivery_status"]
            if state in {"queued", "sending", "sent"}:
                return public(row)
            if state not in {"preview", "failed"} or not old.get("text"):
                raise AppError(409, self.NOT_SENDABLE_CODE, self.NOT_SENDABLE_MESSAGE)
            keys = old.get("_send_keys", [])
            if len(keys) >= 100:
                raise AppError(422, "send_limit", "수동 재전송 요청 한도에 도달했습니다.")
            parts = old["parts"] or self._parts(contents)
            for part in parts:
                if part["status"] != "sent":
                    part.update(status="queued", attempts=0, last_error=None)
            tx.update(
                ref,
                {"delivery_status": "queued", "parts": parts, "_send_keys": [*keys, digest(key)]},
            )
            tx.set(
                out,
                {
                    "owner_uid": uid,
                    "status": "queued",
                    "available_at": now,
                    "lease_token": None,
                    "lease_until": now,
                },
            )
            return {**public(row), "delivery_status": "queued", "parts": parts}

        return write(self.db.transaction())

    def ready(self, uid, now, limit):
        return (
            self.outbox.where(filter=FieldFilter("owner_uid", "==", uid))
            .where(filter=FieldFilter("status", "in", ["queued", "sending"]))
            .where(filter=FieldFilter("available_at", "<=", now))
            .order_by("available_at")
            .limit(limit)
            .stream()
        )

    def claim_part(self, uid, message_id, now):
        out = self.outbox.document(message_id)
        ref = self.messages.document(message_id)

        @firestore.transactional
        def write(tx):
            cooldown = self.destination.get(transaction=tx)
            if cooldown.exists and cooldown.to_dict().get("blocked_until", now) > now:
                return None
            event = self._owned(out, uid, tx).to_dict()
            message = self._owned(ref, uid, tx).to_dict()
            if (
                event["status"] not in {"queued", "sending"}
                or event["available_at"] > now
                or event["lease_until"] > now
            ):
                return None
            parts = message["parts"]
            index = next((i for i, p in enumerate(parts) if p["status"] != "sent"), None)
            if index is None:
                return None
            part = parts[index]
            if event["status"] == "sending":
                part["uncertain"] = True
            if part["attempts"] >= 5:
                part.update(
                    status="failed",
                    last_error={
                        "code": "delivery_uncertain",
                        "message": "전송 확인 없이 재시도 한도에 도달했습니다.",
                    },
                )
                tx.update(out, {"status": "failed"})
                tx.update(ref, {"parts": parts, "delivery_status": "failed"})
                return None
            token = uuid4().hex
            until = now + timedelta(seconds=120)
            part.update(status="sending", attempts=part["attempts"] + 1)
            tx.update(ref, {"parts": parts, "delivery_status": "sending"})
            tx.update(
                out,
                {
                    "status": "sending",
                    "lease_token": token,
                    "lease_until": until,
                    "available_at": until,
                },
            )
            return {
                "id": message_id,
                "token": token,
                "index": index,
                "content": part["content"],
                "attempts": part["attempts"],
            }

        return write(self.db.transaction())

    def _read_success_context(self, tx, message):
        return None

    def _on_success(self, tx, extra, message, now):
        return None

    def finish_part(self, claim, now, *, result=None, error=None):
        out = self.outbox.document(claim["id"])
        ref = self.messages.document(claim["id"])

        @firestore.transactional
        def write(tx):
            event = out.get(transaction=tx).to_dict()
            message = ref.get(transaction=tx).to_dict()
            cooldown = self.destination.get(transaction=tx)
            extra = self._read_success_context(tx, message)
            if (
                event.get("lease_token") != claim["token"]
                or event["status"] != "sending"
                or event["lease_until"] <= now
            ):
                return False
            parts = message["parts"]
            part = parts[claim["index"]]
            available = now
            if result:
                part.update(status="sent", message_id=result["message_id"], last_error=None)
                state = "sent" if all(p["status"] == "sent" for p in parts) else "queued"
            else:
                part.update(
                    last_error={"code": error.code, "message": error.message},
                    uncertain=part["uncertain"] or error.uncertain,
                )
                state = "queued" if error.retryable and part["attempts"] < 5 else "failed"
                part["status"] = state
                available = now + timedelta(
                    seconds=max(
                        1,
                        error.retry_after
                        if error.retry_after is not None
                        else min(900, 30 * 2 ** (part["attempts"] - 1)),
                    )
                )
                if error.code == "discord_rate_limited":
                    existing_until = (
                        cooldown.to_dict().get("blocked_until", now) if cooldown.exists else now
                    )
                    tx.set(self.destination, {"blocked_until": max(available, existing_until)})
            tx.update(
                ref,
                {
                    "parts": parts,
                    "delivery_status": state,
                    "delivered_at": now if state == "sent" else None,
                },
            )
            tx.update(
                out,
                {
                    "status": state,
                    "available_at": available,
                    "lease_token": None,
                    "lease_until": now,
                },
            )
            if state == "sent":
                self._on_success(tx, extra, message, now)
            return True

        return write(self.db.transaction())
