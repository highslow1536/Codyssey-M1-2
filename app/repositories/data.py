import hashlib
import json
from datetime import date as Date
from datetime import datetime, timezone
from uuid import uuid4

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.errors import AppError
from app.pagination import decode_cursor, encode_cursor, scope_key


class DataRepository:
    def __init__(self, db):
        self.db = db
        self.collection = db.collection("data")

    @staticmethod
    def public(snapshot) -> dict:
        data = snapshot.to_dict()
        return {"id": snapshot.id, **{k: v for k, v in data.items() if not k.startswith("_")}}

    def create(self, uid: str, values: dict) -> dict:
        values = dict(values)
        key = values.pop("idempotency_key", None)
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        doc_id = hashlib.sha256(f"{uid}:create:{key}".encode()).hexdigest() if key else uuid4().hex
        ref = self.collection.document(doc_id)

        @firestore.transactional
        def write(transaction):
            snapshot = ref.get(transaction=transaction)
            if snapshot.exists:
                existing = snapshot.to_dict()
                if existing.get("_create_fingerprint") != fingerprint or existing.get("is_deleted"):
                    raise AppError(
                        409, "idempotency_conflict", "같은 키로 다른 요청을 보낼 수 없습니다."
                    )
                return self.public(snapshot)
            now = datetime.now(timezone.utc)
            document = {
                **values,
                "owner_uid": uid,
                "source": "manual",
                "price_basis": "user_value",
                "user_modified": True,
                "is_deleted": False,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "_create_fingerprint": fingerprint,
            }
            transaction.create(ref, document)
            return {"id": doc_id, **{k: v for k, v in document.items() if not k.startswith("_")}}

        return write(self.db.transaction())

    def update(self, uid: str, doc_id: str, values: dict) -> dict:
        ref = self.collection.document(doc_id)

        @firestore.transactional
        def write(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                raise AppError(404, "data_not_found", "데이터를 찾을 수 없습니다.")
            current = snapshot.to_dict()
            if current["owner_uid"] != uid or current.get("is_deleted"):
                raise AppError(404, "data_not_found", "데이터를 찾을 수 없습니다.")
            if current["revision"] != values["expected_revision"]:
                raise AppError(
                    409, "revision_conflict", "다른 변경이 반영되었습니다. 다시 조회하세요."
                )
            updated = {
                **current,
                **{k: v for k, v in values.items() if k != "expected_revision"},
                "revision": current["revision"] + 1,
                "user_modified": True,
                "updated_at": datetime.now(timezone.utc),
            }
            transaction.set(ref, updated)
            return {"id": doc_id, **{k: v for k, v in updated.items() if not k.startswith("_")}}

        return write(self.db.transaction())

    def delete(self, uid: str, doc_id: str):
        ref = self.collection.document(doc_id)

        @firestore.transactional
        def write(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists or snapshot.to_dict()["owner_uid"] != uid:
                raise AppError(404, "data_not_found", "데이터를 찾을 수 없습니다.")
            current = snapshot.to_dict()
            if not current.get("is_deleted"):
                # Preserve the tombstone so a later import cannot resurrect a user's deletion.
                transaction.update(
                    ref,
                    {
                        "is_deleted": True,
                        "revision": current["revision"] + 1,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )

        write(self.db.transaction())

    def query(self, uid, instrument_id, start_date, end_date):
        return (
            self.collection.where(filter=FieldFilter("owner_uid", "==", uid))
            .where(filter=FieldFilter("instrument_id", "==", instrument_id))
            .where(filter=FieldFilter("is_deleted", "==", False))
            .where(filter=FieldFilter("date", ">=", str(start_date)))
            .where(filter=FieldFilter("date", "<=", str(end_date)))
            .order_by("date")
            .order_by("__name__")
        )

    def list(self, uid, instrument_id, start_date, end_date, limit=50, cursor=None):
        query = self.query(uid, instrument_id, start_date, end_date)
        scope = scope_key(uid, instrument_id, start_date, end_date)
        if cursor:
            position = decode_cursor(cursor, scope)
            try:
                doc_id, date = position["id"], position["date"]
                if not isinstance(date, str) or Date.fromisoformat(date).isoformat() != date:
                    raise ValueError
                if not isinstance(doc_id, str) or "/" in doc_id or not doc_id:
                    raise ValueError
                query = query.start_after(
                    {"date": date, "__name__": self.collection.document(doc_id)}
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise AppError(422, "invalid_cursor", "cursor를 확인하세요.") from exc
        snapshots = list(query.limit(limit + 1).stream())
        items = [self.public(snapshot) for snapshot in snapshots[:limit]]
        next_cursor = None
        if len(snapshots) > limit:
            last = items[-1]
            next_cursor = encode_cursor(scope, {"id": last["id"], "date": last["date"]})
        return {"items": items, "next_cursor": next_cursor}

    def all(self, uid, instrument_id, start_date, end_date):
        # A bounded prototype query; never silently summarize only its first page.
        rows = list(self.query(uid, instrument_id, start_date, end_date).limit(5001).stream())
        if len(rows) > 5000:
            raise AppError(422, "too_many_records", "요약할 데이터가 많습니다. 기간을 줄이세요.")
        return [self.public(row) for row in rows]
