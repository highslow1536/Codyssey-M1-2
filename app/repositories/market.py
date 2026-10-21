import hashlib
import json
from datetime import datetime, timezone

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter


def bar_id(bar):
    return hashlib.sha256(
        f"{bar['instrument_id']}:{bar['resolution']}:{bar['date']}".encode()
    ).hexdigest()


class MarketRepository:
    def __init__(self, db):
        self.db = db

    def save_bars(self, uid: str, instrument: dict, bars: list[dict]) -> dict:
        self.db.collection("instruments").document(instrument["id"]).set(instrument)
        counts = {
            "observations": len(bars),
            "market_created": 0,
            "market_updated": 0,
            "data_created": 0,
            "data_updated": 0,
            "data_preserved": 0,
        }
        for offset in range(0, len(bars), 100):
            chunk = bars[offset : offset + 100]
            pairs = []
            for bar in chunk:
                source_id = bar_id(bar)
                data_id = hashlib.sha256(f"{uid}:{source_id}".encode()).hexdigest()
                pairs.append(
                    (
                        bar,
                        self.db.collection("market_bars").document(source_id),
                        self.db.collection("data").document(data_id),
                    )
                )

            @firestore.transactional
            def write(transaction):
                # Firestore requires every read before the first write.
                snapshots = [
                    (
                        bar,
                        market_ref,
                        data_ref,
                        market_ref.get(transaction=transaction),
                        data_ref.get(transaction=transaction),
                    )
                    for bar, market_ref, data_ref in pairs
                ]
                result = dict.fromkeys(counts, 0)
                now = datetime.now(timezone.utc)
                for bar, market_ref, data_ref, previous_market, previous_data in snapshots:
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            {
                                k: bar.get(k)
                                for k in (
                                    "open",
                                    "high",
                                    "low",
                                    "close",
                                    "adjusted_close",
                                    "volume",
                                    "dividends",
                                    "stock_splits",
                                    "currency",
                                    "event_time",
                                )
                            },
                            sort_keys=True,
                            default=str,
                        ).encode()
                    ).hexdigest()
                    old_market = previous_market.to_dict() or {}
                    changed = old_market.get("source_fingerprint") != fingerprint
                    revision = old_market.get("revision", 0) + int(changed)
                    transaction.set(
                        market_ref,
                        {
                            **bar,
                            "revision": revision,
                            "source_fingerprint": fingerprint,
                        },
                    )
                    result[
                        "market_created" if not previous_market.exists else "market_updated"
                    ] += int(changed)
                    old_data = previous_data.to_dict() or {}
                    if old_data.get("user_modified") or old_data.get("is_deleted"):
                        result["data_preserved"] += 1
                        continue
                    if previous_data.exists and old_data.get("source_revision") == revision:
                        continue
                    document = {
                        "owner_uid": uid,
                        "instrument_id": bar["instrument_id"],
                        "date": bar["date"],
                        "value": bar["adjusted_close"],
                        "memo": "",
                        "source": "yahoo",
                        "source_bar_id": market_ref.id,
                        "source_revision": revision,
                        "source_url": bar["source_url"],
                        "event_time": bar["event_time"],
                        "fetched_at": bar["fetched_at"],
                        "currency": bar["currency"],
                        "timezone": bar["timezone"],
                        "price_basis": "adjusted_close",
                        "user_modified": False,
                        "is_deleted": False,
                        "revision": old_data.get("revision", 0) + 1,
                        "created_at": old_data.get("created_at", now),
                        "updated_at": now,
                    }
                    transaction.set(data_ref, document)
                    result["data_updated" if previous_data.exists else "data_created"] += 1
                return result

            chunk_counts = write(self.db.transaction())
            for key in counts:
                if key != "observations":
                    counts[key] += chunk_counts[key]
        return counts

    def history(self, instrument_id, start_date, end_date):
        query = (
            self.db.collection("market_bars")
            .where(filter=FieldFilter("instrument_id", "==", instrument_id))
            .where(filter=FieldFilter("resolution", "==", "1d"))
            .where(filter=FieldFilter("date", ">=", str(start_date)))
            .where(filter=FieldFilter("date", "<=", str(end_date)))
            .order_by("date")
        )
        return [{"id": row.id, **row.to_dict()} for row in query.stream()]
