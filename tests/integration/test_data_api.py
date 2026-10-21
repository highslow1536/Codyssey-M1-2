import json
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog import get_instrument
from app.dependencies import get_current_uid, get_db
from app.main import app
from app.repositories.market import MarketRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def client(firestore_db, owner_uid):
    app.dependency_overrides[get_db] = lambda: firestore_db
    app.dependency_overrides[get_current_uid] = lambda: owner_uid
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_crud_revision_pagination_and_full_summary(client, firestore_db, owner_uid):
    body = {
        "instrument_id": "NASDAQ_AAPL",
        "date": "2025-10-21",
        "value": 100,
        "memo": "Boundary test input",
        "idempotency_key": "create-once",
    }
    created = client.post("/api/data", json=body)
    assert created.status_code == 201, created.text
    doc = created.json()
    assert client.post("/api/data", json=body).json()["id"] == doc["id"]
    assert client.post("/api/data", json={**body, "value": 200}).status_code == 409
    params = {"instrument_id": "NASDAQ_AAPL", "start_date": "2025-10-01", "end_date": "2025-10-31"}
    for day in (22, 23):
        assert (
            client.post(
                "/api/data",
                json={"instrument_id": "NASDAQ_AAPL", "date": f"2025-10-{day}", "value": day},
            ).status_code
            == 201
        )
    first = client.get("/api/data", params={**params, "limit": 1}).json()
    second = client.get(
        "/api/data", params={**params, "limit": 1, "cursor": first["next_cursor"]}
    ).json()
    assert first["items"][0]["id"] != second["items"][0]["id"]
    summary = client.get("/api/data/summary", params=params).json()
    assert summary["count"] == 3
    assert summary["average"] == (100 + 22 + 23) / 3
    changed = client.put(
        f"/api/data/{doc['id']}",
        json={"date": body["date"], "value": 110, "memo": "edited", "expected_revision": 1},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2
    assert (
        client.put(
            f"/api/data/{doc['id']}",
            json={"date": body["date"], "value": 111, "memo": "stale edit", "expected_revision": 1},
        ).status_code
        == 409
    )
    foreign_ref = firestore_db.collection("data").document("foreign-data")
    foreign_ref.set({"owner_uid": "another-user"})
    assert client.delete("/api/data/foreign-data").status_code == 404
    assert client.delete(f"/api/data/{doc['id']}").status_code == 204
    assert client.get("/api/data/summary", params=params).json()["count"] == 2
    assert client.get("/api/data", params={**params, "cursor": "invalid-base64"}).status_code == 422
    foreign_ref.delete()


def test_real_import_deduplicates_and_preserves_edits_and_deletions(firestore_db, owner_uid):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/yahoo_aapl_daily.json").read_text())
    bars = fixture["bars"]
    for bar in bars:
        for key in ("event_time", "fetched_at"):
            bar[key] = datetime.fromisoformat(bar[key])
    repository = MarketRepository(firestore_db)
    instrument = get_instrument("NASDAQ_AAPL")
    first = repository.save_bars(owner_uid, instrument, bars)
    assert first["data_created"] == 3
    assert repository.save_bars(owner_uid, instrument, bars)["data_created"] == 0
    from app.repositories.data import DataRepository

    data = DataRepository(firestore_db)
    records = data.all(owner_uid, "NASDAQ_AAPL", date(2025, 10, 21), date(2025, 10, 31))
    assert len(records) == 3
    edited, deleted = records[:2]
    data.update(
        owner_uid,
        edited["id"],
        {
            "value": 1,
            "memo": "test user edit",
            "date": edited["date"],
            "expected_revision": edited["revision"],
        },
    )
    data.delete(owner_uid, deleted["id"])
    counts = repository.save_bars(owner_uid, instrument, bars)
    assert counts["data_preserved"] == 2
    after = data.all(owner_uid, "NASDAQ_AAPL", date(2025, 10, 21), date(2025, 10, 31))
    assert len(after) == 2
    assert next(row for row in after if row["id"] == edited["id"])["value"] == 1
    originals = repository.history("NASDAQ_AAPL", date(2025, 10, 21), date(2025, 10, 31))
    assert originals[0]["adjusted_close"] == bars[0]["adjusted_close"]
