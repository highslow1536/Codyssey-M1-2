"""Shared public research cache. User-editable data is never used for indicators."""

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from google.cloud import firestore


class ResearchRepository:
    def __init__(self, db):
        self.db = db

    def ref(self, key):
        return self.db.collection("research_cache").document(
            hashlib.sha256(key.encode()).hexdigest()
        )

    def get(self, key):
        return self.ref(key).get().to_dict() or {}

    def acquire(self, key, now, *, refresh):
        """One bounded collector per cache key, including across API processes."""
        ref = self.ref(key)
        token = uuid4().hex

        @firestore.transactional
        def claim(transaction):
            state = ref.get(transaction=transaction).to_dict() or {}
            deadline = state.get("next_attempt_at")
            lease = state.get("lease_until")
            if not refresh or (deadline and deadline > now) or (lease and lease > now):
                return state, None
            transaction.set(
                ref,
                {"key": key, "attempt_token": token, "lease_until": now + timedelta(minutes=3)},
                merge=True,
            )
            return state, token

        return claim(self.db.transaction())

    def finish(self, key, token, now, ttl_minutes, payload=None, error=None):
        ref = self.ref(key)

        @firestore.transactional
        def commit(transaction):
            state = ref.get(transaction=transaction).to_dict() or {}
            if state.get("attempt_token") != token:
                return state
            update = {
                "last_attempt_at": now,
                "next_attempt_at": now + timedelta(minutes=ttl_minutes),
                "lease_until": now,
                "error": error,
            }
            if payload is not None:
                update.update(payload=payload, fetched_at=now, expires_at=update["next_attempt_at"])
            transaction.set(ref, update, merge=True)
            return {**state, **update}

        return commit(self.db.transaction())

    def save_news(self, items):
        # The response cache is capped at 50 bounded articles.
        batch = self.db.batch()
        for article in items:
            values = {
                **article,
                "instrument_ids": firestore.ArrayUnion(article["instrument_ids"]),
                "provider_ids": firestore.ArrayUnion(
                    [f"{article['provider']}:{article['provider_id']}"]
                ),
                "markets": firestore.ArrayUnion([article["market"]]),
            }
            batch.set(
                self.db.collection("news_articles").document(article["id"]), values, merge=True
            )
        if items:
            batch.commit()

    def save_sentiment(self, payload):
        event_time = payload["event_time"]
        if isinstance(event_time, str):
            event_time = datetime.fromisoformat(event_time)
        day = event_time.astimezone(timezone.utc).date().isoformat()
        self.db.collection("sentiment").document(f"US_CNN_FNG_{day}").set(payload)
