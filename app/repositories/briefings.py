"""Transactional schedule slots and briefing history over the shared outbox."""

import json
from datetime import datetime, timedelta
from uuid import uuid4

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.domain.briefing_schedule import next_run
from app.errors import AppError
from app.pagination import decode_cursor, encode_cursor, scope_key
from app.repositories.outbox import OutboxRepository, digest, public


class BriefingRepository(OutboxRepository):
    NOT_FOUND_CODE = "briefing_not_found"
    NOT_FOUND_MESSAGE = "예약 또는 브리핑을 찾을 수 없습니다."
    NOT_SENDABLE_CODE = "briefing_not_sendable"
    NOT_SENDABLE_MESSAGE = "전송할 브리핑을 먼저 생성하세요."

    def __init__(self, db):
        super().__init__(db, collection="briefings", outbox_collection="outbox")
        self.schedules = db.collection("briefing_schedules")
        self.briefings = self.messages

    def _read_success_context(self, tx, message):
        schedule_id = message.get("schedule_id")
        if not schedule_id:
            return None
        ref = self.schedules.document(schedule_id)
        row = ref.get(transaction=tx)
        return (ref, row)

    def _on_success(self, tx, extra, message, now):
        if not extra:
            return
        ref, row = extra
        if not row.exists:
            return
        old_success = row.to_dict().get("last_successful_briefing_at")
        stamp = message["generated_at"]
        if not old_success or stamp > old_success:
            tx.update(ref, {"last_successful_briefing_at": stamp})

    def list_schedules(self, uid):
        rows = self.schedules.where(filter=FieldFilter("owner_uid", "==", uid)).limit(101).stream()
        return sorted(
            [public(row) for row in rows if not row.to_dict().get("_deleted")],
            key=lambda row: row["created_at"],
        )

    def create_schedule(self, uid, values, now):
        values = dict(values)
        key = values.pop("idempotency_key")
        fingerprint = digest(json.dumps(values, sort_keys=True))
        ref = self.schedules.document(digest(uid + ":schedule:" + key))
        upcoming = next_run(values, now)

        @firestore.transactional
        def write(tx):
            old = ref.get(transaction=tx)
            rows = list(
                self.schedules.where(filter=FieldFilter("owner_uid", "==", uid))
                .limit(101)
                .stream(transaction=tx)
            )
            if old.exists:
                if old.to_dict().get("_fingerprint") != fingerprint or old.to_dict().get(
                    "_deleted"
                ):
                    raise AppError(
                        409, "idempotency_conflict", "같은 키로 다른 예약을 만들 수 없습니다."
                    )
                return public(old)
            if len(rows) >= 100:
                raise AppError(422, "schedule_limit", "소유자당 예약 생성은 최대 100개입니다.")
            document = {
                **values,
                "owner_uid": uid,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "next_run_at": upcoming,
                "_fingerprint": fingerprint,
            }
            tx.create(ref, document)
            return {"id": ref.id, **{k: v for k, v in document.items() if not k.startswith("_")}}

        return write(self.db.transaction())

    def update_schedule(self, uid, schedule_id, values, now):
        ref = self.schedules.document(schedule_id)
        values = dict(values)
        revision = values.pop("expected_revision")
        upcoming = next_run(values, now)

        @firestore.transactional
        def write(tx):
            old = self._owned(ref, uid, tx).to_dict()
            if old["revision"] != revision:
                raise AppError(409, "revision_conflict", "예약이 변경되었습니다. 다시 불러오세요.")
            updated = {
                **old,
                **values,
                "revision": revision + 1,
                "updated_at": now,
                "next_run_at": upcoming,
            }
            tx.set(ref, updated)
            return {"id": ref.id, **{k: v for k, v in updated.items() if not k.startswith("_")}}

        return write(self.db.transaction())

    def delete_schedule(self, uid, schedule_id, now):
        ref = self.schedules.document(schedule_id)

        @firestore.transactional
        def write(tx):
            old = self._owned(ref, uid, tx).to_dict()
            tx.update(
                ref,
                {
                    "active": False,
                    "_deleted": True,
                    "revision": old["revision"] + 1,
                    "updated_at": now,
                },
            )

        write(self.db.transaction())

    def list(self, uid, limit=20, cursor=None):
        query = (
            self.briefings.where(filter=FieldFilter("owner_uid", "==", uid))
            .order_by("generated_at", direction=firestore.Query.DESCENDING)
            .order_by("__name__", direction=firestore.Query.DESCENDING)
        )
        scope = scope_key(uid, "briefings")
        if cursor:
            position = decode_cursor(cursor, scope)
            try:
                stamp = datetime.fromisoformat(position["at"])
                doc_id = position["id"]
                if not stamp.tzinfo or not isinstance(doc_id, str) or not doc_id or "/" in doc_id:
                    raise ValueError
                query = query.start_after(
                    {"generated_at": stamp, "__name__": self.briefings.document(doc_id)}
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise AppError(422, "invalid_cursor", "cursor를 확인하세요.") from exc
        rows = list(query.limit(limit + 1).stream())
        items = [public(row) for row in rows[:limit]]
        cursor = (
            encode_cursor(
                scope, {"at": items[-1]["generated_at"].isoformat(), "id": items[-1]["id"]}
            )
            if len(rows) > limit
            else None
        )
        return {"items": items, "next_cursor": cursor}

    def save_preview(self, uid, selection, content):
        ref = self.briefings.document(uuid4().hex)
        ref.create(
            {
                **selection,
                **content,
                "owner_uid": uid,
                "delivery_status": "preview",
                "parts": [],
                "schedule_id": None,
                "slot": None,
            }
        )
        return self.get(uid, ref.id)

    def due(self, uid, now, limit):
        return (
            self.schedules.where(filter=FieldFilter("owner_uid", "==", uid))
            .where(filter=FieldFilter("active", "==", True))
            .where(filter=FieldFilter("next_run_at", "<=", now))
            .order_by("next_run_at")
            .limit(limit)
            .stream()
        )

    def reserve(self, uid, schedule_id, now):
        ref = self.schedules.document(schedule_id)
        row = ref.get()
        schedule = row.to_dict() if row.exists else None
        if not schedule or schedule["owner_uid"] != uid or schedule.get("_deleted"):
            return None
        if not schedule["active"] or schedule["next_run_at"] > now:
            return None
        slot = schedule["next_run_at"]
        target = self.briefings.document(digest(schedule_id + ":" + slot.isoformat()))
        upcoming = next_run(schedule, now)
        token = uuid4().hex
        document = {
            "owner_uid": uid,
            "market": schedule["market"],
            "instrument_ids": schedule["instrument_ids"],
            "generated_at": now,
            "delivery_status": "building",
            "schedule_id": schedule_id,
            "slot": slot,
            "parts": [],
            "text": "",
            "sources": [],
            "warnings": [],
            "data_as_of": [],
            "_schedule": schedule,
            "_lease_token": token,
            "_lease_until": now + timedelta(minutes=10),
        }
        try:
            target.create(document)
            created = True
        except AlreadyExists:
            created = False
            existing = target.get().to_dict()
            if not existing or existing["delivery_status"] == "building":
                return None

        @firestore.transactional
        def advance(tx):
            current_row = ref.get(transaction=tx)
            claim_row = target.get(transaction=tx)
            current = current_row.to_dict() if current_row.exists else None
            claimed = claim_row.to_dict() if claim_row.exists else None
            if not claimed:
                return False
            if created and claimed.get("_lease_token") != token:
                return False
            if (
                not current
                or current.get("_deleted")
                or not current["active"]
                or current["revision"] != schedule["revision"]
                or current["next_run_at"] != slot
            ):
                if created:
                    tx.update(target, {"delivery_status": "cancelled"})
                return False
            tx.update(ref, {"next_run_at": upcoming})
            return created

        return {"id": target.id, **document} if advance(self.db.transaction()) else None

    def recover_builds(self, uid, now, limit):
        return (
            self.briefings.where(filter=FieldFilter("owner_uid", "==", uid))
            .where(filter=FieldFilter("delivery_status", "==", "building"))
            .where(filter=FieldFilter("_lease_until", "<=", now))
            .limit(limit)
            .stream()
        )

    def reclaim(self, uid, briefing_id, now):
        ref = self.briefings.document(briefing_id)

        @firestore.transactional
        def write(tx):
            old = self._owned(ref, uid, tx).to_dict()
            if old["delivery_status"] != "building" or old["_lease_until"] > now:
                return None
            old.update(_lease_token=uuid4().hex, _lease_until=now + timedelta(minutes=10))
            tx.update(
                ref, {"_lease_token": old["_lease_token"], "_lease_until": old["_lease_until"]}
            )
            return {"id": ref.id, **old}

        return write(self.db.transaction())

    def finish_build(self, claim, content, parts, now):
        ref = self.briefings.document(claim["id"])

        @firestore.transactional
        def write(tx):
            old = ref.get(transaction=tx).to_dict()
            schedule_ref = self.schedules.document(old["schedule_id"])
            schedule = schedule_ref.get(transaction=tx).to_dict()
            if (
                old["delivery_status"] != "building"
                or old.get("_lease_token") != claim["_lease_token"]
                or old["_lease_until"] <= now
            ):
                return False
            if (
                not schedule
                or not schedule["active"]
                or schedule["revision"] != old["_schedule"]["revision"]
            ):
                tx.update(ref, {"delivery_status": "cancelled"})
                return False
            if content is None:
                tx.update(
                    ref,
                    {
                        "delivery_status": "failed",
                        "warnings": ["브리핑 생성에 실패했습니다. 다음 예약에서 다시 수집합니다."],
                    },
                )
                return False
            tx.update(ref, {**content, "delivery_status": "queued", "parts": self._parts(parts)})
            tx.create(
                self.outbox.document(ref.id),
                {
                    "owner_uid": old["owner_uid"],
                    "status": "queued",
                    "available_at": now,
                    "lease_token": None,
                    "lease_until": now,
                },
            )
            return True

        return write(self.db.transaction())
