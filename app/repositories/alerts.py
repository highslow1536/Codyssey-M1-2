"""Owner-scoped rules, atomic condition events, and shared Discord delivery."""

import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.domain.alerts import evaluate, initial_state
from app.errors import AppError
from app.pagination import decode_cursor, encode_cursor, scope_key
from app.repositories.outbox import OutboxRepository


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def visible(row):
    return {"id": row.id, **{k: v for k, v in row.to_dict().items() if not k.startswith("_")}}


def fingerprint(values):
    return digest(json.dumps(values, sort_keys=True, default=str))


class AlertRepository(OutboxRepository):
    def __init__(self, db):
        super().__init__(db, "alert_events", "alert_outbox")
        self.rules = db.collection("alerts")
        self.streams = db.collection("price_streams")
        self.quotes = db.collection("price_quotes")

    def _rule(self, uid, alert_id, tx=None):
        row = self.rules.document(alert_id).get(transaction=tx)
        if not row.exists or row.to_dict().get("owner_uid") != uid or row.to_dict().get("_deleted"):
            raise AppError(404, "alert_not_found", "가격 알림 규칙을 찾을 수 없습니다.")
        return row

    def list_rules(self, uid):
        rows = self.rules.where(filter=FieldFilter("owner_uid", "==", uid)).limit(101).stream()
        return sorted(
            [visible(r) for r in rows if not r.to_dict().get("_deleted")],
            key=lambda r: r["created_at"],
            reverse=True,
        )

    def get_rule(self, uid, alert_id):
        return visible(self._rule(uid, alert_id))

    @staticmethod
    def _expiry(values, now):
        if values.get("expires_at") and values["expires_at"] <= now:
            raise AppError(422, "invalid_expiry", "만료 시각은 현재보다 이후여야 합니다.")

    def create(self, uid, values, now):
        values = dict(values)
        key = values.pop("idempotency_key")
        ref = self.rules.document(digest(f"{uid}:alert:{key}"))
        stamp = fingerprint(values)

        @firestore.transactional
        def write(tx):
            old = ref.get(transaction=tx)
            rows = list(
                self.rules.where(filter=FieldFilter("owner_uid", "==", uid))
                .limit(101)
                .stream(transaction=tx)
            )
            if old.exists:
                if old.to_dict().get("_fingerprint") != stamp or old.to_dict().get("_deleted"):
                    raise AppError(
                        409, "idempotency_conflict", "같은 키로 다른 규칙을 만들 수 없습니다."
                    )
                return visible(old)
            self._expiry(values, now)
            if len(rows) >= 100:
                raise AppError(422, "alert_limit", "소유자당 알림 규칙은 최대 100개입니다.")
            data = {
                **values,
                "owner_uid": uid,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "state": initial_state(now),
                "monitor_status": "waiting" if values["active"] else "paused",
                "last_error": None,
                "_fingerprint": stamp,
                "_generation": 1,
            }
            tx.create(ref, data)
            return {"id": ref.id, **{k: v for k, v in data.items() if not k.startswith("_")}}

        return write(self.db.transaction())

    def update(self, uid, alert_id, values, now):
        values = dict(values)
        revision = values.pop("expected_revision")
        reset = values.pop("reset_state", False)
        ref = self.rules.document(alert_id)

        @firestore.transactional
        def write(tx):
            row = self._rule(uid, alert_id, tx)
            old = row.to_dict()
            if old["revision"] != revision:
                raise AppError(409, "revision_conflict", "규칙이 변경되었습니다. 다시 불러오세요.")
            changed = any(
                values[k] != old[k]
                for k in (
                    "instrument_id",
                    "currency",
                    "steps",
                    "session_scope",
                    "repeat_mode",
                    "step_timeout_hours",
                )
            )
            if changed and not reset:
                raise AppError(
                    409, "reset_required", "조건을 바꾸려면 진행 상태 초기화를 선택하세요."
                )
            if values["active"]:
                self._expiry(values, now)
            state = initial_state(now) if reset else old["state"]
            if not reset and not old["active"] and values["active"]:
                # Paused observations cannot establish a crossing. Keep armed progress.
                state = {
                    **state,
                    "started_at": now,
                    "last_event_time": None,
                    "last_price_decimal": None,
                    "gap_pending": True,
                }
            data = {
                **old,
                **values,
                "revision": revision + 1,
                "updated_at": now,
                "state": state,
                "_generation": old["_generation"] + int(reset),
                "last_error": None,
                "monitor_status": "paused"
                if not values["active"]
                else "completed"
                if state["completed"]
                else "waiting",
            }
            tx.set(ref, data)
            return {"id": ref.id, **{k: v for k, v in data.items() if not k.startswith("_")}}

        return write(self.db.transaction())

    def reset(self, uid, alert_id, revision, now):
        old = visible(self._rule(uid, alert_id))
        fields = (
            "instrument_id",
            "currency",
            "steps",
            "session_scope",
            "repeat_mode",
            "step_timeout_hours",
            "expires_at",
            "active",
        )
        return self.update(
            uid,
            alert_id,
            {
                **{k: old[k] for k in fields},
                "active": True,
                "expected_revision": revision,
                "reset_state": True,
            },
            now,
        )

    def delete(self, uid, alert_id, now):
        @firestore.transactional
        def write(tx):
            row = self._rule(uid, alert_id, tx)
            tx.update(
                row.reference,
                {
                    "active": False,
                    "_deleted": True,
                    "revision": row.to_dict()["revision"] + 1,
                    "monitor_status": "cancelled",
                    "updated_at": now,
                },
            )

        write(self.db.transaction())

    def status(self, uid, alert_id, status, now, error=None, *, pause=False, revision=None):
        @firestore.transactional
        def write(tx):
            row = self._rule(uid, alert_id, tx)
            old = row.to_dict()
            if (
                not old["active"]
                or old["state"]["completed"]
                or (revision is not None and old["revision"] != revision)
            ):
                return
            fields = {"monitor_status": status, "last_error": error, "last_checked_at": now}
            if old.get("monitor_status") == status and old.get("last_error") == error and not pause:
                return
            if pause:
                fields.update(active=False, revision=old["revision"] + 1)
            tx.update(row.reference, fields)

        try:
            write(self.db.transaction())
        except AppError as exc:
            if exc.status_code != 404:
                raise

    def apply(self, uid, alert_id, quotes, now, build_event, *, revision=None):
        """At most 80 ordered observations per transaction; no HTTP inside it."""
        ref = self.rules.document(alert_id)

        @firestore.transactional
        def write(tx):
            old = self._rule(uid, alert_id, tx).to_dict()
            if (
                not old["active"]
                or old["state"]["completed"]
                or (revision is not None and old["revision"] != revision)
            ):
                return 0
            if old.get("expires_at") and old["expires_at"] <= now:
                tx.update(ref, {"active": False, "monitor_status": "expired"})
                return 0
            state = old["state"]
            events = []
            for quote in sorted(quotes, key=lambda q: q["event_time"])[:80]:
                if (
                    quote["instrument_id"] != old["instrument_id"]
                    or quote["currency"] != old["currency"]
                ):
                    raise AppError(
                        422, "quote_mismatch", "규칙과 시세 종목·통화가 일치하지 않습니다."
                    )
                # Corporate actions invalidate fixed thresholds even if the provider is fresh.
                watermark = (
                    state.get("last_event_time") or state.get("started_at") or old["created_at"]
                )
                if quote["event_time"] > watermark and (
                    quote.get("stock_splits", 0) or quote.get("dividends", 0)
                ):
                    tx.update(
                        ref,
                        {
                            "active": False,
                            "monitor_status": "corporate_action",
                            "revision": old["revision"] + 1,
                            "last_error": {
                                "code": "corporate_action",
                                "message": "분할·배당 등 기업행위를 확인했습니다. 문턱값 확인 후 초기화하세요.",
                            },
                        },
                    )
                    return 0
                state, trigger = evaluate(old, state, quote)
                if trigger:
                    event_id = digest(
                        f"{alert_id}:{old['_generation']}:{trigger['cycle']}:{trigger['event_time'].isoformat()}"
                    )
                    events.append((event_id, build_event(old, trigger, quote, event_id, now)))
                if state["completed"]:
                    break
            for event_id, event in events:
                tx.create(
                    self.messages.document(event_id),
                    {**event, "alert_id": alert_id, "owner_uid": uid},
                )
                tx.create(
                    self.outbox.document(event_id),
                    {
                        "owner_uid": uid,
                        "status": "queued",
                        "available_at": now,
                        "lease_token": None,
                        "lease_until": now,
                    },
                )
            tx.update(
                ref,
                {
                    "state": state,
                    "monitor_status": "completed"
                    if state["completed"]
                    else "armed"
                    if state["stage"]
                    else "watching",
                    "last_error": None,
                    "last_checked_at": now,
                },
            )
            return len(events)

        try:
            return write(self.db.transaction())
        except AppError as exc:
            if exc.status_code == 404:
                return 0
            raise

    def list_events(self, uid, limit=20, cursor=None):
        query = (
            self.messages.where(filter=FieldFilter("owner_uid", "==", uid))
            .order_by("generated_at", direction=firestore.Query.DESCENDING)
            .order_by("__name__", direction=firestore.Query.DESCENDING)
        )
        scope = scope_key(uid, "alert-events")
        if cursor:
            position = decode_cursor(cursor, scope)
            try:
                at, doc_id = datetime.fromisoformat(position["at"]), position["id"]
                if not at.tzinfo or not isinstance(doc_id, str) or not doc_id or "/" in doc_id:
                    raise ValueError
            except (ValueError, KeyError, TypeError) as exc:
                raise AppError(422, "invalid_cursor", "전송 이력 cursor를 확인하세요.") from exc
            query = query.start_after(
                {"generated_at": at, "__name__": self.messages.document(doc_id)}
            )
        rows = list(query.limit(limit + 1).stream())
        items = [visible(r) for r in rows[:limit]]
        next_cursor = (
            encode_cursor(
                scope, {"at": items[-1]["generated_at"].isoformat(), "id": items[-1]["id"]}
            )
            if len(rows) > limit
            else None
        )
        return {"items": items, "next_cursor": next_cursor}

    def claim_poll(self, instrument_id, now, poll_minutes):
        ref = self.streams.document(instrument_id)

        @firestore.transactional
        def write(tx):
            row = ref.get(transaction=tx)
            old = row.to_dict() if row.exists else {}
            if old.get("next_poll_at", now) > now or old.get("lease_until", now) > now:
                return None
            token = uuid4().hex
            tx.set(
                ref,
                {
                    **old,
                    "lease_token": token,
                    "lease_until": now + timedelta(seconds=120),
                    "next_poll_at": now + timedelta(minutes=poll_minutes),
                },
            )
            return token

        return write(self.db.transaction())

    def finish_poll(self, instrument_id, token, now, quotes=None, error=None):
        ref = self.streams.document(instrument_id)
        old = ref.get().to_dict() or {}
        if old.get("lease_token") != token or old.get("lease_until", now) <= now:
            return False
        # Persist only newly observed bars; repeated five-day downloads do not
        # rewrite hundreds of historical documents on every five-minute poll.
        if quotes:
            quotes = sorted(quotes, key=lambda q: q["event_time"])
            watermark = old.get("latest_event_time")
            pending = [q for q in quotes if not watermark or q["event_time"] > watermark]
            for offset in range(0, len(pending), 400):
                batch = self.db.batch()
                for quote in pending[offset : offset + 400]:
                    batch.set(
                        self.quotes.document(
                            digest(f"{instrument_id}:{quote['event_time'].isoformat()}")
                        ),
                        quote,
                    )
                batch.commit()

        @firestore.transactional
        def write(tx):
            old = ref.get(transaction=tx).to_dict() or {}
            if old.get("lease_token") != token or old["lease_until"] <= now:
                return False
            fields = {
                "lease_token": None,
                "lease_until": now,
                "fetched_at": now,
                "last_error": error,
            }
            if quotes and (
                not old.get("latest_event_time")
                or quotes[-1]["event_time"] >= old["latest_event_time"]
            ):
                fields["latest_event_time"] = quotes[-1]["event_time"]
                fields["latest_quote"] = quotes[-1]
            tx.update(ref, fields)
            return True

        return write(self.db.transaction())

    def latest_quote(self, instrument_id):
        return (self.streams.document(instrument_id).get().to_dict() or {}).get("latest_quote")

    def cached_quotes(self, instrument_id, now, since=None):
        return [
            r.to_dict()
            for r in self.quotes.where(filter=FieldFilter("instrument_id", "==", instrument_id))
            .where(
                filter=FieldFilter(
                    "event_time",
                    ">=",
                    max(since or now - timedelta(days=8), now - timedelta(days=8)),
                )
            )
            .order_by("event_time")
            .limit(2500)
            .stream()
        ]
