"""Transactional schedule slots and a durable, fenced Discord outbox."""

import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.domain.briefing_schedule import next_run
from app.errors import AppError
from app.pagination import decode_cursor, encode_cursor, scope_key


def public(snapshot):
    return {
        "id": snapshot.id,
        **{k: v for k, v in snapshot.to_dict().items() if not k.startswith("_")},
    }


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class BriefingRepository:
    def __init__(self, db):
        self.db = db
        self.schedules = db.collection("briefing_schedules")
        self.briefings = db.collection("briefings")
        self.outbox = db.collection("outbox")
        self.destination = db.collection("delivery_destinations").document("default")

    def _owned(self, ref, uid, transaction=None):
        snapshot = ref.get(transaction=transaction)
        if (
            not snapshot.exists
            or snapshot.to_dict()["owner_uid"] != uid
            or snapshot.to_dict().get("_deleted")
        ):
            raise AppError(404, "briefing_not_found", "예약 또는 브리핑을 찾을 수 없습니다.")
        return snapshot

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
        ref = self.schedules.document(digest(f"{uid}:schedule:{key}"))
        upcoming = next_run(values, now)

        @firestore.transactional
        def write(tx):
            old = ref.get(transaction=tx)
            # Bound schedules per owner, including inactive/deleted tombstones.
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

    def get(self, uid, briefing_id):
        return public(self._owned(self.briefings.document(briefing_id), uid))

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
        target = self.briefings.document(digest(f"{schedule_id}:{slot.isoformat()}"))
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
        # Firestore create has an exists=false precondition: exactly one worker
        # wins without eight read-lock holders competing to upgrade a schedule.
        try:
            target.create(document)
            created = True
        except AlreadyExists:
            created = False
            existing = target.get().to_dict()
            if not existing or existing["delivery_status"] == "building":
                return None
            # Repair a crash after slot creation and before schedule advancement.

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

    def queue(self, uid, briefing_id, key, contents, now):
        ref, out = self.briefings.document(briefing_id), self.outbox.document(briefing_id)

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
                raise AppError(409, "briefing_not_sendable", "전송할 브리핑을 먼저 생성하세요.")
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

    def claim_part(self, uid, briefing_id, now):
        out, ref = self.outbox.document(briefing_id), self.briefings.document(briefing_id)

        @firestore.transactional
        def write(tx):
            cooldown = self.destination.get(transaction=tx)
            if cooldown.exists and cooldown.to_dict().get("blocked_until", now) > now:
                return None
            event = self._owned(out, uid, tx).to_dict()
            briefing = self._owned(ref, uid, tx).to_dict()
            if (
                event["status"] not in {"queued", "sending"}
                or event["available_at"] > now
                or event["lease_until"] > now
            ):
                return None
            parts = briefing["parts"]
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
                "id": briefing_id,
                "token": token,
                "index": index,
                "content": part["content"],
                "attempts": part["attempts"],
            }

        return write(self.db.transaction())

    def finish_part(self, claim, now, *, result=None, error=None):
        out, ref = self.outbox.document(claim["id"]), self.briefings.document(claim["id"])

        @firestore.transactional
        def write(tx):
            event = out.get(transaction=tx).to_dict()
            briefing = ref.get(transaction=tx).to_dict()
            schedule_ref = (
                self.schedules.document(briefing["schedule_id"])
                if briefing.get("schedule_id")
                else None
            )
            schedule = schedule_ref.get(transaction=tx) if schedule_ref else None
            cooldown = self.destination.get(transaction=tx)
            if (
                event.get("lease_token") != claim["token"]
                or event["status"] != "sending"
                or event["lease_until"] <= now
            ):
                return False
            parts = briefing["parts"]
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
            if state == "sent" and schedule and schedule.exists:
                old_success = schedule.to_dict().get("last_successful_briefing_at")
                stamp = briefing["generated_at"]
                if not old_success or stamp > old_success:
                    tx.update(schedule_ref, {"last_successful_briefing_at": stamp})
            return True

        return write(self.db.transaction())
