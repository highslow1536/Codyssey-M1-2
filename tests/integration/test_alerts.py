"""Emulator-backed integration tests for price alerts.

Observable behavior only: rule CRUD with owner fencing/revision/idempotency,
preview without persistence, explicit-reset gating, pause/resume without
inferred crossings, reset rearm, expiry, atomic two-stage persistence,
equality crossings, duplicate/out-of-order/restart/repeat, small-herd
concurrency (2-4 workers), history preservation, owner-scoped cursors,
shared 429 cooldown with briefings, worker tick with explicit doubles at
frozen 2026-10-22T01:30Z (KR regular), stale/empty handling, poll lease and
incremental cache. Generic outbox fencing is covered by the shared suite.
Real Firestore emulator, frozen UTC clocks, no network, no mock paths.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_current_uid, get_db
from app.errors import AppError
from app.main import app
from app.repositories.alerts import AlertRepository
from app.repositories.briefings import BriefingRepository
from app.services.alerts import AlertService, event_content

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 22, 1, 30, 0, tzinfo=timezone.utc)
WEBHOOK = "https://discord.com/api/webhooks/123/test-only"
INSTRUMENT = "KRX_005930"
KST = ZoneInfo("Asia/Seoul")
SOURCE_URL = "https://finance.yahoo.com/quote/005930.KS"


def _settings(**over):
    kw = {"_env_file": None, "discord_webhook_url": WEBHOOK}
    kw.update(over)
    return Settings(**kw)


class FakeProvider:
    def __init__(self, quotes=None, error=None):
        self._quotes = list(quotes or [])
        self._error = error
        self.calls = []

    def fetch(self, instrument, now):
        self.calls.append((dict(instrument), now))
        if self._error:
            raise self._error
        return [dict(q) for q in self._quotes]


class FakeDiscord:
    def __init__(self, error=None):
        self.sent = []
        self._error = error

    def send(self, content):
        if self._error:
            raise self._error
        self.sent.append(content)
        return {"message_id": "mid-%d" % len(self.sent)}


def _session_date(moment):
    return moment.astimezone(KST).date().isoformat()


def _quote(price, moment, **over):
    base = {
        "instrument_id": INSTRUMENT,
        "currency": "KRW",
        "price_decimal": str(price),
        "event_time": moment,
        "bar_start": moment - timedelta(minutes=5),
        "fetched_at": moment,
        "session_date": _session_date(moment),
        "source_url": SOURCE_URL,
        "delay_seconds": 1200,
        "quality": "delayed",
        "provider": "yahoo",
        "resolution": "5m",
        "session": "regular",
        "timezone": "Asia/Seoul",
        "volume": 100,
        "dividends": 0.0,
        "stock_splits": 0.0,
        "corporate_action": False,
        "gap": False,
    }
    base.update(over)
    return base


def _values(key, **over):
    vals = {
        "instrument_id": INSTRUMENT,
        "currency": "KRW",
        "steps": [{"op": "cross_above", "price": "35000"}, {"op": "lte", "price": "30000"}],
        "session_scope": "regular",
        "repeat_mode": "once",
        "expires_at": None,
        "step_timeout_hours": None,
        "active": True,
        "idempotency_key": key,
    }
    vals.update(over)
    return vals


def _full_update(revision, reset=False, **over):
    vals = {
        "instrument_id": INSTRUMENT,
        "currency": "KRW",
        "steps": [{"op": "cross_above", "price": "35000"}, {"op": "lte", "price": "30000"}],
        "session_scope": "regular",
        "repeat_mode": "once",
        "expires_at": None,
        "step_timeout_hours": None,
        "active": True,
        "expected_revision": revision,
        "reset_state": reset,
    }
    vals.update(over)
    return vals


def _wipe_collection(db, name, field=None, value=None):
    col = db.collection(name)
    if field is None:
        for doc in col.stream():
            doc.reference.delete()
        return
    from google.cloud.firestore_v1.base_query import FieldFilter

    for doc in col.where(filter=FieldFilter(field, "==", value)).stream():
        doc.reference.delete()


@pytest.fixture
def api_client(firestore_db, owner_uid):
    app.dependency_overrides[get_db] = lambda: firestore_db
    app.dependency_overrides[get_current_uid] = lambda: owner_uid
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_shared(firestore_db):
    firestore_db.collection("delivery_destinations").document("default").delete()
    firestore_db.collection("price_streams").document(INSTRUMENT).delete()
    yield
    firestore_db.collection("delivery_destinations").document("default").delete()
    firestore_db.collection("price_streams").document(INSTRUMENT).delete()
    _wipe_collection(firestore_db, "price_quotes", "instrument_id", INSTRUMENT)


def _seed_poll(db, quotes, when=NOW):
    repo = AlertRepository(db)
    token = repo.claim_poll(INSTRUMENT, when, 5)
    assert token is not None
    assert repo.finish_poll(INSTRUMENT, token, when, quotes=quotes) is True
    return repo


def test_rule_create_idempotent_revision_owner(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    first = repo.create(owner_uid, _values("rule-1"), NOW)
    assert first["revision"] == 1
    assert first["owner_uid"] == owner_uid
    again = repo.create(owner_uid, _values("rule-1"), NOW)
    assert again["id"] == first["id"]
    assert again["revision"] == 1
    with pytest.raises(AppError) as exc:
        repo.create(owner_uid, _values("rule-1", steps=[{"op": "gte", "price": "36000"}]), NOW)
    assert exc.value.code == "idempotency_conflict"
    updated = repo.update(owner_uid, first["id"], _full_update(1, active=False), NOW)
    assert updated["revision"] == 2
    assert updated["active"] is False
    with pytest.raises(AppError) as exc:
        repo.update(owner_uid, first["id"], _full_update(1, active=True), NOW)
    assert exc.value.code == "revision_conflict"
    with pytest.raises(AppError) as exc:
        repo.update(owner_uid + "-other", first["id"], _full_update(2, active=True), NOW)
    assert exc.value.code in {"alert_not_found", "message_not_found"}
    assert [r["id"] for r in repo.list_rules(owner_uid)] == [first["id"]]
    assert repo.list_rules(owner_uid + "-other") == []
    repo.delete(owner_uid, first["id"], NOW)
    assert repo.list_rules(owner_uid) == []
    with pytest.raises(AppError) as exc:
        repo.update(owner_uid, first["id"], _full_update(2, active=True), NOW)
    assert exc.value.code in {"alert_not_found", "message_not_found"}


def test_rule_api_crud_owner_revision_idempotency(firestore_db, owner_uid, api_client):
    body = {
        "instrument_id": INSTRUMENT,
        "currency": "KRW",
        "steps": [{"op": "cross_above", "price": "35000"}, {"op": "lte", "price": "30000"}],
        "session_scope": "regular",
        "repeat_mode": "once",
        "expires_at": None,
        "step_timeout_hours": None,
        "active": True,
        "idempotency_key": "api-rule-1",
    }
    created = api_client.post("/api/alerts", json=body)
    assert created.status_code == 201, created.text
    alert_id = created.json()["id"]
    assert created.json()["revision"] == 1
    listed = api_client.get("/api/alerts")
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()["items"]] == [alert_id]
    dup = api_client.post("/api/alerts", json=body)
    assert dup.status_code == 201
    assert dup.json()["id"] == alert_id
    conflict = api_client.post(
        "/api/alerts", json={**body, "steps": [{"op": "gte", "price": "36000"}]}
    )
    assert conflict.status_code == 409
    bad_precision = api_client.post(
        "/api/alerts",
        json={
            **body,
            "idempotency_key": "api-bad-prec",
            "steps": [{"op": "gte", "price": "35000.5"}],
        },
    )
    assert bad_precision.status_code == 422
    bad_op = api_client.post(
        "/api/alerts",
        json={**body, "idempotency_key": "api-bad-op", "steps": [{"op": "nope", "price": "35000"}]},
    )
    assert bad_op.status_code == 422
    update_body = {
        "instrument_id": INSTRUMENT,
        "currency": "KRW",
        "steps": [{"op": "cross_above", "price": "35000"}, {"op": "lte", "price": "30000"}],
        "session_scope": "regular",
        "repeat_mode": "once",
        "expires_at": None,
        "step_timeout_hours": None,
        "active": False,
        "expected_revision": 1,
        "reset_state": False,
    }
    paused = api_client.patch(f"/api/alerts/{alert_id}", json=update_body)
    assert paused.status_code == 200, paused.text
    assert paused.json()["revision"] == 2
    stale = api_client.patch(
        f"/api/alerts/{alert_id}", json={**update_body, "expected_revision": 1}
    )
    assert stale.status_code == 409
    reset = api_client.post(f"/api/alerts/{alert_id}/reset", json={"expected_revision": 2})
    assert reset.status_code == 200
    assert reset.json()["revision"] == 3
    assert reset.json()["active"] is True
    deleted = api_client.delete(f"/api/alerts/{alert_id}")
    assert deleted.status_code == 204
    assert api_client.get("/api/alerts").json()["items"] == []
    app.dependency_overrides.clear()
    with TestClient(app) as anon:
        assert anon.get("/api/alerts").status_code == 401
        assert anon.get("/api/alert-events").status_code == 401


def test_preview_no_persistence_and_validation(firestore_db, owner_uid, api_client):
    payload = {
        "instrument_id": INSTRUMENT,
        "currency": "KRW",
        "steps": [{"op": "cross_above", "price": "35000"}, {"op": "lte", "price": "30000"}],
        "session_scope": "regular",
        "repeat_mode": "once",
        "expires_at": None,
        "step_timeout_hours": None,
        "active": True,
    }
    resp = api_client.post("/api/alerts/preview", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "35000" in data["description"] and "30000" in data["description"]
    assert data["observation"]["interval_minutes"] == 5
    repo = AlertRepository(firestore_db)
    assert repo.list_rules(owner_uid) == []
    assert repo.list_events(owner_uid)["items"] == []
    with_key = dict(payload, idempotency_key="must-fail")
    assert api_client.post("/api/alerts/preview", json=with_key).status_code == 422


def test_condition_change_requires_explicit_reset(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(owner_uid, _values("cond-1"), NOW)
    with pytest.raises(AppError) as exc:
        repo.update(
            owner_uid,
            rule["id"],
            _full_update(1, steps=[{"op": "cross_above", "price": "36000"}]),
            NOW,
        )
    assert exc.value.code == "reset_required"
    ok = repo.update(
        owner_uid,
        rule["id"],
        _full_update(1, True, steps=[{"op": "cross_above", "price": "36000"}]),
        NOW,
    )
    assert ok["revision"] == 2
    assert ok["steps"] == [{"op": "cross_above", "price": "36000"}]
    assert ok["state"]["stage"] == 0


def test_pause_resume_no_inferred_crossing(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(
        owner_uid,
        _values("pause-1", steps=[{"op": "cross_above", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    t0 = NOW - timedelta(minutes=20)
    t1 = NOW - timedelta(minutes=15)
    t2 = NOW - timedelta(minutes=10)
    assert repo.apply(owner_uid, rule["id"], [_quote("34900", t0)], NOW, event_content) == 0
    pause_steps = [{"op": "cross_above", "price": "35000"}]
    repo.update(owner_uid, rule["id"], _full_update(1, active=False, steps=pause_steps), t1)
    assert repo.apply(owner_uid, rule["id"], [_quote("35100", t1)], NOW, event_content) == 0
    resumed = repo.update(
        owner_uid, rule["id"], _full_update(2, active=True, steps=pause_steps), t2
    )
    assert resumed["active"] is True
    assert repo.apply(owner_uid, rule["id"], [_quote("35100", t2)], NOW, event_content) == 0
    assert repo.list_events(owner_uid)["items"] == []
    t3 = NOW - timedelta(minutes=5)
    assert repo.apply(owner_uid, rule["id"], [_quote("34900", t3)], NOW, event_content) == 0
    assert repo.apply(owner_uid, rule["id"], [_quote("35100", NOW)], NOW, event_content) == 1


def test_reset_rearms_after_completion(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(
        owner_uid,
        _values("rearm-1", steps=[{"op": "gte", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    base = NOW - timedelta(minutes=10)
    assert repo.apply(owner_uid, rule["id"], [_quote("36000", base)], NOW, event_content) == 1
    stored = repo.list_rules(owner_uid)[0]
    assert stored["state"]["completed"] is True
    assert repo.apply(owner_uid, rule["id"], [_quote("37000", NOW)], NOW, event_content) == 0
    rearmed = repo.reset(owner_uid, rule["id"], stored["revision"], NOW + timedelta(minutes=1))
    assert rearmed["state"]["completed"] is False
    assert rearmed["state"]["stage"] == 0
    assert (
        repo.apply(
            owner_uid, rule["id"], [_quote("36000", NOW + timedelta(minutes=2))], NOW, event_content
        )
        == 1
    )


def test_expiry_rejects_past_and_event_time_fail_safe(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    with pytest.raises(AppError) as exc:
        repo.create(owner_uid, _values("exp-past", expires_at=NOW - timedelta(seconds=1)), NOW)
    assert exc.value.code == "invalid_expiry"
    rule = repo.create(owner_uid, _values("exp-1", expires_at=NOW + timedelta(minutes=30)), NOW)
    late = NOW + timedelta(hours=1)
    assert repo.apply(owner_uid, rule["id"], [_quote("36000", late)], NOW, event_content) == 0
    assert repo.list_events(owner_uid)["items"] == []


def test_apply_atomic_two_stage_sequence(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(owner_uid, _values("seq-1"), NOW - timedelta(hours=1))
    t0 = NOW - timedelta(minutes=15)
    t1 = NOW - timedelta(minutes=10)
    t2 = NOW - timedelta(minutes=5)
    batch = [_quote("35000", t0), _quote("35001", t1), _quote("30000", t2)]
    assert repo.apply(owner_uid, rule["id"], batch, NOW, event_content) == 1
    events = repo.list_events(owner_uid)["items"]
    assert len(events) == 1
    event = events[0]
    assert event["price_decimal"] == "30000"
    assert len(event["evidence"]) == 2
    assert event["evidence"][0]["price_decimal"] == "35001"
    stored = repo.list_rules(owner_uid)[0]
    assert stored["state"]["completed"] is True
    assert stored["state"]["stage"] == 2


def test_equality_crossings(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    up = repo.create(
        owner_uid,
        _values("eq-up", steps=[{"op": "cross_above", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    t0 = NOW - timedelta(minutes=10)
    t1 = NOW - timedelta(minutes=5)
    assert repo.apply(owner_uid, up["id"], [_quote("35000", t0)], NOW, event_content) == 0
    assert repo.apply(owner_uid, up["id"], [_quote("35001", t1)], NOW, event_content) == 1
    down = repo.create(
        owner_uid,
        _values("eq-down", steps=[{"op": "cross_below", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    assert repo.apply(owner_uid, down["id"], [_quote("35000", t0)], NOW, event_content) == 0
    assert repo.apply(owner_uid, down["id"], [_quote("34999", t1)], NOW, event_content) == 1


def test_duplicate_outoforder_restart_no_retrigger(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(
        owner_uid,
        _values("dedupe-1", steps=[{"op": "gte", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    t0 = NOW - timedelta(minutes=10)
    assert repo.apply(owner_uid, rule["id"], [_quote("36000", t0)], NOW, event_content) == 1
    assert repo.apply(owner_uid, rule["id"], [_quote("36000", t0)], NOW, event_content) == 0
    fresh = repo.create(
        owner_uid,
        _values("dedupe-3", steps=[{"op": "cross_above", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    base = NOW - timedelta(minutes=30)
    mid = NOW - timedelta(minutes=25)
    assert repo.apply(owner_uid, fresh["id"], [_quote("34900", base)], NOW, event_content) == 0
    assert repo.apply(owner_uid, fresh["id"], [_quote("35100", mid)], NOW, event_content) == 1
    assert repo.apply(owner_uid, fresh["id"], [_quote("35200", base)], NOW, event_content) == 0
    mine = [e for e in repo.list_events(owner_uid)["items"] if e["alert_id"] == fresh["id"]]
    assert len(mine) == 1


def test_repeat_mode_cycles(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(
        owner_uid,
        _values("repeat-1", steps=[{"op": "gte", "price": "35000"}], repeat_mode="repeat"),
        NOW - timedelta(hours=1),
    )
    t0 = NOW - timedelta(minutes=10)
    t1 = NOW - timedelta(minutes=5)
    assert repo.apply(owner_uid, rule["id"], [_quote("36000", t0)], NOW, event_content) == 1
    assert repo.apply(owner_uid, rule["id"], [_quote("36100", t1)], NOW, event_content) == 1
    stored = repo.list_rules(owner_uid)[0]
    assert stored["state"]["completed"] is False
    assert stored["state"]["cycle"] == 2
    mine = [e for e in repo.list_events(owner_uid)["items"] if e["alert_id"] == rule["id"]]
    assert {e["cycle"] for e in mine} == {0, 1}


def test_concurrent_apply_single_event(firestore_db, owner_uid):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    repo = AlertRepository(firestore_db)
    rule = repo.create(
        owner_uid,
        _values("herd-1", steps=[{"op": "gte", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    quote = _quote("36000", NOW - timedelta(minutes=5))
    barrier = threading.Barrier(3)

    def attempt(_):
        barrier.wait(timeout=30)
        return AlertRepository(firestore_db).apply(
            owner_uid, rule["id"], [quote], NOW, event_content
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(attempt, range(3)))
    assert sum(results) == 1
    mine = [e for e in repo.list_events(owner_uid)["items"] if e["alert_id"] == rule["id"]]
    assert len(mine) == 1


def test_delete_preserves_history(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(
        owner_uid,
        _values("del-1", steps=[{"op": "gte", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    assert repo.apply(owner_uid, rule["id"], [_quote("36000", NOW)], NOW, event_content) == 1
    event_id = repo.list_events(owner_uid)["items"][0]["id"]
    repo.delete(owner_uid, rule["id"], NOW)
    assert repo.list_rules(owner_uid) == []
    kept = repo.get(owner_uid, event_id)
    assert kept["id"] == event_id
    assert repo.list_events(owner_uid)["items"][0]["id"] == event_id
    with pytest.raises(AppError):
        repo.get(owner_uid + "-other", event_id)


def test_history_pagination_owner_cursor_detail(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(
        owner_uid,
        _values("page-1", steps=[{"op": "gte", "price": "35000"}], repeat_mode="repeat"),
        NOW - timedelta(hours=1),
    )
    for i in range(3):
        moment = NOW - timedelta(minutes=30 - i * 5)
        assert (
            repo.apply(owner_uid, rule["id"], [_quote(str(36000 + i), moment)], NOW, event_content)
            == 1
        )
    page1 = repo.list_events(owner_uid, limit=2)
    assert len(page1["items"]) == 2
    assert page1["next_cursor"]
    assert page1["items"][0]["generated_at"] >= page1["items"][1]["generated_at"]
    page2 = repo.list_events(owner_uid, limit=2, cursor=page1["next_cursor"])
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None
    assert len({i["id"] for i in page1["items"]} | {i["id"] for i in page2["items"]}) == 3
    assert repo.list_events(owner_uid + "-other", limit=20)["items"] == []
    with pytest.raises(AppError) as exc:
        repo.list_events(owner_uid + "-other", limit=20, cursor=page1["next_cursor"])
    assert exc.value.code == "invalid_cursor"
    detail = repo.get(owner_uid, page1["items"][0]["id"])
    assert detail["id"] == page1["items"][0]["id"]
    with pytest.raises(AppError):
        repo.get(owner_uid + "-other", page1["items"][0]["id"])


def test_delivery_429_cooldown_shared_with_briefings(firestore_db, owner_uid):
    alerts = AlertRepository(firestore_db)
    rule = alerts.create(
        owner_uid,
        _values("cool-1", steps=[{"op": "gte", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    assert alerts.apply(owner_uid, rule["id"], [_quote("36000", NOW)], NOW, event_content) == 1
    event_id = alerts.list_events(owner_uid)["items"][0]["id"]
    alerts.queue(owner_uid, event_id, "cool-key", ["only-part"], NOW)
    claim = alerts.claim_part(owner_uid, event_id, NOW)
    assert claim is not None
    limited = SimpleNamespace(
        code="discord_rate_limited",
        message="slow down",
        retryable=True,
        retry_after=120,
        uncertain=False,
    )
    assert alerts.finish_part(claim, NOW, error=limited) is True
    dest = firestore_db.collection("delivery_destinations").document("default").get()
    assert dest.exists
    assert dest.to_dict()["blocked_until"] >= NOW + timedelta(seconds=120)
    assert alerts.claim_part(owner_uid, event_id, NOW + timedelta(seconds=30)) is None
    briefings = BriefingRepository(firestore_db)
    preview = briefings.save_preview(
        owner_uid,
        {"market": "KR", "instrument_ids": []},
        {"text": "bounded", "sources": [], "warnings": [], "data_as_of": [], "generated_at": NOW},
    )
    briefings.queue(owner_uid, preview["id"], "share-1", ["one part"], NOW)
    assert briefings.claim_part(owner_uid, preview["id"], NOW + timedelta(seconds=30)) is None
    after = alerts.claim_part(owner_uid, event_id, NOW + timedelta(seconds=121))
    assert after is not None
    assert after["index"] == 0


def test_queue_idempotency(firestore_db, owner_uid):
    alerts = AlertRepository(firestore_db)
    rule = alerts.create(
        owner_uid,
        _values("qid-1", steps=[{"op": "gte", "price": "35000"}]),
        NOW - timedelta(hours=1),
    )
    assert alerts.apply(owner_uid, rule["id"], [_quote("36000", NOW)], NOW, event_content) == 1
    event_id = alerts.list_events(owner_uid)["items"][0]["id"]
    first = alerts.queue(owner_uid, event_id, "same-key", ["hello"], NOW)
    assert first["delivery_status"] == "queued"
    second = alerts.queue(owner_uid, event_id, "same-key", ["hello"], NOW)
    assert second["delivery_status"] == "queued"


def _tick_service(db, provider, discord, clock):
    return AlertService(db, _settings(), provider=provider, discord=discord, clock=clock)


def test_tick_delivers_with_real_metadata_and_counts(firestore_db, owner_uid):
    quotes = [
        _quote("35000", NOW - timedelta(minutes=15)),
        _quote("35001", NOW - timedelta(minutes=10)),
        _quote("30000", NOW - timedelta(minutes=5)),
    ]
    repo = AlertRepository(firestore_db)
    repo.create(owner_uid, _values("tick-1"), NOW - timedelta(minutes=20))
    svc = _tick_service(firestore_db, FakeProvider(quotes), FakeDiscord(), lambda: NOW)
    counts = svc.tick(owner_uid)
    assert counts["quotes_fetched"] == len(quotes)
    assert counts["alerts_triggered"] == 1
    assert counts["monitor_errors"] == 0
    assert counts["parts_sent"] >= 1
    assert counts["delivery_errors"] == 0
    events = repo.list_events(owner_uid)["items"]
    assert len(events) == 1
    event = events[0]
    assert event["instrument_id"] == INSTRUMENT
    assert event["source_url"] == SOURCE_URL
    assert event["delivery_status"] == "sent"
    assert "35000" in event["description"]
    again = _tick_service(firestore_db, FakeProvider(quotes), FakeDiscord(), lambda: NOW).tick(
        owner_uid
    )
    assert again["alerts_triggered"] == 0
    assert again["parts_sent"] == 0


def test_tick_stale_and_empty_provider(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    repo.create(owner_uid, _values("stale-1"), NOW - timedelta(minutes=60))
    old = _quote("35000", NOW - timedelta(days=2))
    _seed_poll(firestore_db, [old], NOW - timedelta(days=2))
    svc = _tick_service(firestore_db, FakeProvider([]), FakeDiscord(), lambda: NOW)
    counts = svc.tick(owner_uid)
    assert counts["alerts_triggered"] == 0
    assert counts["monitor_errors"] >= 1
    stored = repo.list_rules(owner_uid)[0]
    assert stored["monitor_status"] in {"stale", "unavailable", "market_closed"}
    assert repo.list_events(owner_uid)["items"] == []
    _wipe_collection(firestore_db, "price_quotes", "instrument_id", INSTRUMENT)
    firestore_db.collection("price_streams").document(INSTRUMENT).delete()
    counts2 = _tick_service(firestore_db, FakeProvider([]), FakeDiscord(), lambda: NOW).tick(
        owner_uid
    )
    assert counts2["alerts_triggered"] == 0


def test_poll_lease_incremental_cache_and_since_bound(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    q1 = _quote("35000", NOW - timedelta(minutes=20))
    q2 = _quote("35001", NOW - timedelta(minutes=10))
    token = repo.claim_poll(INSTRUMENT, NOW, 5)
    assert token
    assert repo.claim_poll(INSTRUMENT, NOW, 5) is None
    assert repo.finish_poll(INSTRUMENT, token, NOW, quotes=[q1, q2]) is True
    assert repo.finish_poll(INSTRUMENT, "wrong-token", NOW, quotes=[q1]) is False
    latest = repo.latest_quote(INSTRUMENT)
    assert latest is not None
    assert latest["price_decimal"] == "35001"
    q3 = _quote("35002", NOW - timedelta(minutes=5))
    token2 = repo.claim_poll(INSTRUMENT, NOW + timedelta(minutes=6), 5)
    assert token2
    before = len(repo.cached_quotes(INSTRUMENT, NOW + timedelta(minutes=6)))
    assert (
        repo.finish_poll(INSTRUMENT, token2, NOW + timedelta(minutes=6), quotes=[q1, q2, q3])
        is True
    )
    after = repo.cached_quotes(INSTRUMENT, NOW + timedelta(minutes=6))
    assert len(after) == before + 1
    assert after[-1]["price_decimal"] == "35002"
    tail = repo.cached_quotes(
        INSTRUMENT, NOW + timedelta(minutes=6), since=NOW - timedelta(minutes=7)
    )
    assert [q["price_decimal"] for q in tail] == ["35002"]
    assert repo.latest_quote(INSTRUMENT)["price_decimal"] == "35002"
    token3 = repo.claim_poll(INSTRUMENT, NOW + timedelta(minutes=12), 5)
    assert token3
    assert (
        repo.finish_poll(
            INSTRUMENT, token3, NOW + timedelta(minutes=12) + timedelta(seconds=121), quotes=[q3]
        )
        is False
    )


def test_tick_uses_earliest_watermark(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    repo.create(owner_uid, _values("wm-1"), NOW - timedelta(minutes=30))
    repo.create(
        owner_uid,
        _values("wm-2", steps=[{"op": "gte", "price": "99999"}]),
        NOW - timedelta(minutes=30),
    )
    quotes = [
        _quote("35000", NOW - timedelta(minutes=15)),
        _quote("35001", NOW - timedelta(minutes=10)),
        _quote("30000", NOW - timedelta(minutes=5)),
    ]
    svc = _tick_service(firestore_db, FakeProvider(quotes), FakeDiscord(), lambda: NOW)
    counts = svc.tick(owner_uid)
    assert counts["alerts_triggered"] == 1
    assert len(repo.list_events(owner_uid)["items"]) == 1


def test_api_expiry_json_currency_and_partial_patch(
    firestore_db, owner_uid, api_client, monkeypatch
):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr("app.api.alerts.datetime", FrozenDateTime)
    aware_iso = (NOW + timedelta(hours=2)).isoformat()
    body = {
        "instrument_id": INSTRUMENT,
        "currency": "KRW",
        "steps": [{"op": "gte", "price": "35000"}],
        "session_scope": "regular",
        "repeat_mode": "once",
        "expires_at": aware_iso,
        "step_timeout_hours": None,
        "active": True,
        "idempotency_key": "api-exp-json-1",
    }
    created = api_client.post("/api/alerts", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["expires_at"] is not None
    naive = body.copy()
    naive["idempotency_key"] = "api-exp-naive-1"
    naive["expires_at"] = "2026-10-23T01:30:00"
    assert api_client.post("/api/alerts", json=naive).status_code == 422
    mismatch = dict(body, idempotency_key="api-cur-mismatch-1", currency="USD")
    assert api_client.post("/api/alerts", json=mismatch).status_code == 422
    alert_id = created.json()["id"]
    partial = api_client.patch(
        f"/api/alerts/{alert_id}", json={"active": False, "expected_revision": 1}
    )
    assert partial.status_code == 422


def test_corporate_action_pauses_without_partially_emitting_events(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(owner_uid, _values("split-pause"), NOW - timedelta(hours=1))
    quotes = [
        _quote("35000", NOW - timedelta(minutes=15)),
        _quote("35001", NOW - timedelta(minutes=10)),
        _quote("29000", NOW - timedelta(minutes=5), stock_splits=2.0),
    ]
    assert repo.apply(owner_uid, rule["id"], quotes, NOW, event_content) == 0
    stored = repo.list_rules(owner_uid)[0]
    assert stored["active"] is False
    assert stored["monitor_status"] == "corporate_action"
    assert stored["last_error"]["code"] == "corporate_action"
    assert repo.list_events(owner_uid)["items"] == []


def test_resumed_armed_rule_labels_first_level_hit_after_pause(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(owner_uid, _values("armed-resume"), NOW - timedelta(hours=1))
    repo.apply(
        owner_uid,
        rule["id"],
        [
            _quote("35000", NOW - timedelta(minutes=25)),
            _quote("35001", NOW - timedelta(minutes=20)),
        ],
        NOW - timedelta(minutes=20),
        event_content,
    )
    repo.update(owner_uid, rule["id"], _full_update(1, active=False), NOW - timedelta(minutes=15))
    resumed = repo.update(
        owner_uid, rule["id"], _full_update(2, active=True), NOW - timedelta(minutes=10)
    )
    assert resumed["state"]["stage"] == 1
    assert (
        repo.apply(
            owner_uid, rule["id"], [_quote("30000", NOW - timedelta(minutes=5))], NOW, event_content
        )
        == 1
    )
    event = repo.list_events(owner_uid)["items"][0]
    assert event["gap"] is True
    assert "공백 이후 첫 관측" in event["text"]


def test_stale_revision_cannot_apply_old_instrument_observations(firestore_db, owner_uid):
    repo = AlertRepository(firestore_db)
    rule = repo.create(owner_uid, _values("edit-fence"), NOW - timedelta(hours=1))
    repo.update(owner_uid, rule["id"], _full_update(1, reset=True), NOW - timedelta(minutes=20))
    assert (
        repo.apply(
            owner_uid,
            rule["id"],
            [
                _quote("35000", NOW - timedelta(minutes=15)),
                _quote("35001", NOW - timedelta(minutes=10)),
                _quote("30000", NOW - timedelta(minutes=5)),
            ],
            NOW,
            event_content,
            revision=1,
        )
        == 0
    )
    assert repo.list_rules(owner_uid)[0]["state"]["last_event_time"] is None
    assert repo.list_events(owner_uid)["items"] == []
