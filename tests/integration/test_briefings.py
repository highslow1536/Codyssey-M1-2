"""Emulator-backed integration tests for briefing schedules and Discord outbox.

Observable behavior only: schedule idempotency/revision/owner fencing,
preview/history pagination with owner-scoped cursors, queue gating plus
send-key idempotency, slot uniqueness, generation fencing/recovery,
multi-part retry semantics, 429 cooldown, attempt cap, sender-lease
fencing, and failed-retry preservation. Real Firestore emulator, frozen
UTC clocks, test-only research/discord doubles, no network.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_current_uid, get_db
from app.errors import AppError
from app.main import app
from app.repositories.briefings import BriefingRepository
from app.services.briefings import BriefingService

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 22, 0, 0, 0, tzinfo=timezone.utc)
WEBHOOK = "https://discord.com/api/webhooks/123/test-only"


def _settings(**over):
    kw = {"_env_file": None, "discord_webhook_url": WEBHOOK}
    kw.update(over)
    return Settings(**kw)


class FakeResearch:
    def __init__(self, now):
        self.now = now

    def analysis(self, instrument_id):
        return {
            "quality_status": "ok",
            "latest_close": 100.0,
            "returns_pct": {"1": 0.5},
            "realized_volatility_20d_pct": 12.0,
            "rsi_14": 55.0,
            "data_as_of": self.now.isoformat(),
            "end_date": self.now.date().isoformat(),
            "source_urls": ["https://example.com/analysis"],
        }

    def news(self, market, instrument_id=None, window="24h", *args, **kwargs):
        return {"quality_status": "ok", "items": [], "truncated": False}

    def cnn_sentiment(self, *args, **kwargs):
        return {
            "quality_status": "ok",
            "value": 55.0,
            "rating": "Neutral",
            "event_time": self.now.isoformat(),
            "source_url": "https://www.cnn.com/markets/fear-and-greed",
        }


class FakeDiscord:
    def __init__(self):
        self.sent = []

    def send(self, content):
        self.sent.append(content)
        return {"message_id": "123"}


@pytest.fixture
def api_client(firestore_db, owner_uid):
    app.dependency_overrides[get_db] = lambda: firestore_db
    app.dependency_overrides[get_current_uid] = lambda: owner_uid
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_cooldown(firestore_db):
    ref = firestore_db.collection("delivery_destinations").document("default")
    ref.delete()
    yield
    ref.delete()


def _sched(key, **over):
    vals = {
        "market": "KR",
        "instrument_ids": ["KRX_005930"],
        "schedule_kind": "fixed_time",
        "timezone": "Asia/Seoul",
        "local_time": "09:00",
        "weekdays": [0, 1, 2, 3, 4],
        "close_offset_minutes": 40,
        "active": True,
        "destination_alias": "default",
        "idempotency_key": key,
    }
    vals.update(over)
    return vals


def _update_vals(revision, **over):
    vals = {
        "market": "KR",
        "instrument_ids": ["KRX_005930"],
        "schedule_kind": "fixed_time",
        "timezone": "Asia/Seoul",
        "local_time": "09:00",
        "weekdays": [0, 1, 2, 3, 4],
        "close_offset_minutes": 40,
        "active": True,
        "destination_alias": "default",
        "expected_revision": revision,
    }
    vals.update(over)
    return vals


def _content(text="bounded briefing body", when=NOW):
    return {
        "text": text,
        "sources": [],
        "warnings": [],
        "data_as_of": [],
        "generated_at": when,
    }


def _raw_briefing(firestore_db, bid):
    return firestore_db.collection("briefings").document(bid).get().to_dict()


def test_schedule_create_idempotent_revision_and_owner(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    first = repo.create_schedule(owner_uid, _sched("sched-1"), NOW)
    assert first["revision"] == 1
    assert first["owner_uid"] == owner_uid

    again = repo.create_schedule(owner_uid, _sched("sched-1"), NOW)
    assert again["id"] == first["id"]
    assert again["revision"] == 1

    with pytest.raises(AppError) as exc:
        repo.create_schedule(owner_uid, _sched("sched-1", local_time="10:00"), NOW)
    assert exc.value.code == "idempotency_conflict"

    updated = repo.update_schedule(owner_uid, first["id"], _update_vals(1, local_time="10:00"), NOW)
    assert updated["revision"] == 2
    assert updated["local_time"] == "10:00"

    with pytest.raises(AppError) as exc:
        repo.update_schedule(owner_uid, first["id"], _update_vals(1), NOW)
    assert exc.value.code == "revision_conflict"

    with pytest.raises(AppError) as exc:
        repo.update_schedule(owner_uid + "-other", first["id"], _update_vals(2), NOW)
    assert exc.value.code == "briefing_not_found"

    listed = repo.list_schedules(owner_uid)
    assert [row["id"] for row in listed] == [first["id"]]
    assert repo.list_schedules(owner_uid + "-other") == []

    repo.delete_schedule(owner_uid, first["id"], NOW)
    assert repo.list_schedules(owner_uid) == []
    with pytest.raises(AppError) as exc:
        repo.update_schedule(owner_uid, first["id"], _update_vals(2), NOW)
    assert exc.value.code == "briefing_not_found"


def test_schedule_api_validation_and_missing_auth(firestore_db, owner_uid, api_client):
    dup = {
        "market": "KR",
        "instrument_ids": ["KRX_005930", "KRX_005930"],
        "schedule_kind": "fixed_time",
        "timezone": "Asia/Seoul",
        "local_time": "09:00",
        "weekdays": [0, 1, 2, 3, 4],
        "close_offset_minutes": 40,
        "active": True,
        "destination_alias": "default",
        "idempotency_key": "api-bad-1",
    }
    assert api_client.post("/api/briefing-schedules", json=dup).status_code == 422

    mismatch = dict(dup, instrument_ids=["NASDAQ_AAPL"], idempotency_key="api-bad-2")
    assert api_client.post("/api/briefing-schedules", json=mismatch).status_code == 422

    app.dependency_overrides.clear()
    with TestClient(app) as anon:
        assert anon.get("/api/briefings").status_code == 401
        assert anon.get("/api/briefing-schedules").status_code == 401


def test_preview_history_pagination_and_owner_cursor(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    for i in range(3):
        moment = NOW + timedelta(seconds=i)
        svc = BriefingService(
            firestore_db,
            _settings(),
            research_factory=lambda moment=moment: FakeResearch(moment),
            discord=FakeDiscord(),
            clock=lambda moment=moment: moment,
        )
        saved = svc.preview(owner_uid, {"market": "KR", "instrument_ids": ["KRX_005930"]})
        assert saved["delivery_status"] == "preview"
        assert "시장 브리핑" in saved["text"]

    page1 = repo.list(owner_uid, 2)
    assert len(page1["items"]) == 2
    assert page1["next_cursor"]
    assert page1["items"][0]["generated_at"] >= page1["items"][1]["generated_at"]
    page2 = repo.list(owner_uid, 2, page1["next_cursor"])
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None
    seen = {item["id"] for item in page1["items"]} | {item["id"] for item in page2["items"]}
    assert len(seen) == 3

    assert repo.list(owner_uid + "-other", 20)["items"] == []
    with pytest.raises(AppError) as exc:
        repo.list(owner_uid + "-other", 20, page1["next_cursor"])
    assert exc.value.code == "invalid_cursor"

    detail = repo.get(owner_uid, page1["items"][0]["id"])
    assert detail["id"] == page1["items"][0]["id"]
    with pytest.raises(AppError) as exc:
        repo.get(owner_uid + "-other", page1["items"][0]["id"])
    assert exc.value.code == "briefing_not_found"


def test_queue_requires_webhook_and_stays_idempotent(firestore_db, owner_uid, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    blank = Settings(_env_file=None, discord_webhook_url=None)
    repo = BriefingRepository(firestore_db)
    prev = repo.save_preview(owner_uid, {"market": "KR", "instrument_ids": []}, _content())
    gated = BriefingService(
        firestore_db,
        blank,
        research_factory=lambda: FakeResearch(NOW),
        discord=FakeDiscord(),
        clock=lambda: NOW,
    )
    with pytest.raises(AppError) as exc:
        gated.queue(owner_uid, prev["id"], "k-1")
    assert exc.value.code == "discord_not_configured"
    assert exc.value.status_code == 503

    svc = BriefingService(
        firestore_db,
        _settings(),
        research_factory=lambda: FakeResearch(NOW),
        discord=FakeDiscord(),
        clock=lambda: NOW,
    )
    first = svc.queue(owner_uid, prev["id"], "k-1")
    assert first["delivery_status"] == "queued"
    assert len(first["parts"]) >= 1
    second = svc.queue(owner_uid, prev["id"], "k-1")
    assert second["delivery_status"] == "queued"
    third = svc.queue(owner_uid, prev["id"], "k-2")
    assert third["delivery_status"] == "queued"
    assert len(_raw_briefing(firestore_db, prev["id"])["_send_keys"]) == 1

    empty = repo.save_preview(owner_uid, {"market": "KR", "instrument_ids": []}, _content(text=""))
    with pytest.raises(AppError) as exc:
        svc.queue(owner_uid, empty["id"], "k-empty")
    assert exc.value.code == "briefing_not_sendable"


def test_reserve_slot_concurrent_single_winner(firestore_db, owner_uid):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    repo = BriefingRepository(firestore_db)
    sched = repo.create_schedule(owner_uid, _sched("slot-1"), NOW)
    firestore_db.collection("briefing_schedules").document(sched["id"]).update({"next_run_at": NOW})
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait(timeout=30)
        return BriefingRepository(firestore_db).reserve(owner_uid, sched["id"], NOW)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: attempt(), range(8)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0]["slot"] == NOW
    advanced = firestore_db.collection("briefing_schedules").document(sched["id"]).get().to_dict()
    assert advanced["next_run_at"] > NOW


def test_expired_generation_reclaim_fenced(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    sched = repo.create_schedule(owner_uid, _sched("gen-1"), NOW)
    firestore_db.collection("briefing_schedules").document(sched["id"]).update({"next_run_at": NOW})
    claim = repo.reserve(owner_uid, sched["id"], NOW)
    assert claim is not None
    assert repo.reclaim(owner_uid, claim["id"], NOW + timedelta(minutes=1)) is None

    later = NOW + timedelta(minutes=11)
    reclaimed = repo.reclaim(owner_uid, claim["id"], later)
    assert reclaimed is not None
    assert reclaimed["_lease_token"] != claim["_lease_token"]

    stale = repo.finish_build(claim, _content("stale build", later), ["stale-part"], later)
    assert stale is False
    ok = repo.finish_build(reclaimed, _content("fresh bounded build", later), ["part-a"], later)
    assert ok is True
    assert repo.get(owner_uid, claim["id"])["delivery_status"] == "queued"


def test_multipart_partial_failure_retries_only_unsent(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    prev = repo.save_preview(owner_uid, {"market": "KR", "instrument_ids": []}, _content())
    queued = repo.queue(owner_uid, prev["id"], "mk-1", ["part-one", "part-two"], NOW)
    assert len(queued["parts"]) == 2

    first = repo.claim_part(owner_uid, prev["id"], NOW)
    assert first["index"] == 0
    assert repo.finish_part(first, NOW, result={"message_id": "123"}) is True

    second = repo.claim_part(owner_uid, prev["id"], NOW)
    assert second["index"] == 1
    transient = SimpleNamespace(
        code="discord_unavailable",
        message="boom",
        retryable=True,
        retry_after=None,
        uncertain=True,
    )
    assert repo.finish_part(second, NOW, error=transient) is True
    row = repo.get(owner_uid, prev["id"])
    assert row["parts"][0]["status"] == "sent"
    assert row["parts"][1]["status"] == "queued"

    retry = repo.claim_part(owner_uid, prev["id"], NOW + timedelta(seconds=31))
    assert retry is not None
    assert retry["index"] == 1
    assert (
        repo.finish_part(retry, NOW + timedelta(seconds=31), result={"message_id": "124"}) is True
    )
    done = repo.get(owner_uid, prev["id"])
    assert done["delivery_status"] == "sent"
    assert [part["status"] for part in done["parts"]] == ["sent", "sent"]


def test_rate_limited_cooldown_blocks_and_releases(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    prev = repo.save_preview(owner_uid, {"market": "KR", "instrument_ids": []}, _content())
    repo.queue(owner_uid, prev["id"], "rl-1", ["only-part"], NOW)
    claim = repo.claim_part(owner_uid, prev["id"], NOW)
    assert claim is not None
    limited = SimpleNamespace(
        code="discord_rate_limited",
        message="slow down",
        retryable=True,
        retry_after=120,
        uncertain=False,
    )
    assert repo.finish_part(claim, NOW, error=limited) is True
    dest = firestore_db.collection("delivery_destinations").document("default").get()
    assert dest.exists
    assert dest.to_dict()["blocked_until"] >= NOW + timedelta(seconds=120)

    assert repo.claim_part(owner_uid, prev["id"], NOW + timedelta(seconds=30)) is None
    after = repo.claim_part(owner_uid, prev["id"], NOW + timedelta(seconds=121))
    assert after is not None
    assert after["index"] == 0


def test_attempt_cap_marks_failed(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    prev = repo.save_preview(owner_uid, {"market": "KR", "instrument_ids": []}, _content())
    repo.queue(owner_uid, prev["id"], "cap-1", ["cap-part"], NOW)
    moment = NOW
    for _ in range(5):
        claim = repo.claim_part(owner_uid, prev["id"], moment)
        assert claim is not None
        down = SimpleNamespace(
            code="discord_unavailable",
            message="down",
            retryable=True,
            retry_after=0,
            uncertain=True,
        )
        assert repo.finish_part(claim, moment, error=down) is True
        moment = moment + timedelta(seconds=1)
    assert repo.claim_part(owner_uid, prev["id"], moment) is None
    assert repo.get(owner_uid, prev["id"])["delivery_status"] == "failed"


def test_expired_sender_lease_marks_uncertain_and_fences(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    prev = repo.save_preview(owner_uid, {"market": "KR", "instrument_ids": []}, _content())
    repo.queue(owner_uid, prev["id"], "lease-1", ["lease-part"], NOW)
    old = repo.claim_part(owner_uid, prev["id"], NOW)
    assert old is not None

    late = NOW + timedelta(seconds=121)
    fresh = repo.claim_part(owner_uid, prev["id"], late)
    assert fresh is not None
    assert fresh["token"] != old["token"]
    assert repo.get(owner_uid, prev["id"])["parts"][0]["uncertain"] is True

    assert repo.finish_part(old, late, result={"message_id": "123"}) is False
    assert repo.finish_part(fresh, late, result={"message_id": "123"}) is True
    assert repo.get(owner_uid, prev["id"])["delivery_status"] == "sent"


def test_manual_retry_after_failure_preserves_sent(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    prev = repo.save_preview(owner_uid, {"market": "KR", "instrument_ids": []}, _content())
    repo.queue(owner_uid, prev["id"], "rk-1", ["keep", "redo"], NOW)
    first = repo.claim_part(owner_uid, prev["id"], NOW)
    assert repo.finish_part(first, NOW, result={"message_id": "m0"}) is True
    second = repo.claim_part(owner_uid, prev["id"], NOW)
    refused = SimpleNamespace(
        code="discord_forbidden", message="no", retryable=False, retry_after=None, uncertain=False
    )
    assert repo.finish_part(second, NOW, error=refused) is True
    failed = repo.get(owner_uid, prev["id"])
    assert failed["delivery_status"] == "failed"
    assert failed["parts"][0]["status"] == "sent"

    retried = repo.queue(
        owner_uid, prev["id"], "rk-2", ["keep", "redo"], NOW + timedelta(minutes=5)
    )
    assert retried["parts"][0]["status"] == "sent"
    assert retried["parts"][0]["message_id"] == "m0"
    assert retried["parts"][1]["status"] == "queued"
    assert retried["parts"][1]["attempts"] == 0
    assert len(_raw_briefing(firestore_db, prev["id"])["_send_keys"]) == 2


def test_tick_generates_delivers_once_and_watermarks(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    sched = repo.create_schedule(owner_uid, _sched("tick-1"), NOW)
    firestore_db.collection("briefing_schedules").document(sched["id"]).update({"next_run_at": NOW})

    def make_service():
        return BriefingService(
            firestore_db,
            _settings(),
            research_factory=lambda: FakeResearch(NOW),
            discord=FakeDiscord(),
            clock=lambda: NOW,
        )

    counts = make_service().tick(owner_uid)
    assert counts["generated"] == 1
    assert counts["parts_sent"] >= 1

    again = make_service().tick(owner_uid)
    assert again == {"generated": 0, "parts_sent": 0, "delivery_errors": 0}

    rows = repo.list(owner_uid, 20)["items"]
    assert len(rows) == 1
    assert rows[0]["delivery_status"] == "sent"
    watermark = firestore_db.collection("briefing_schedules").document(sched["id"]).get().to_dict()
    assert watermark["last_successful_briefing_at"] == rows[0]["generated_at"]


def test_edit_during_generation_cancels_before_queue(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    sched = repo.create_schedule(owner_uid, _sched("cancel-1"), NOW)
    firestore_db.collection("briefing_schedules").document(sched["id"]).update({"next_run_at": NOW})
    claim = repo.reserve(owner_uid, sched["id"], NOW)
    assert claim is not None

    repo.update_schedule(owner_uid, sched["id"], _update_vals(1, local_time="10:00"), NOW)
    ok = repo.finish_build(
        claim, _content("late build", NOW), ["late-part"], NOW + timedelta(seconds=1)
    )
    assert ok is False
    row = repo.get(owner_uid, claim["id"])
    assert row["delivery_status"] == "cancelled"
    assert firestore_db.collection("outbox").document(claim["id"]).get().exists is False


def test_recovers_crash_between_atomic_slot_creation_and_schedule_advance(firestore_db, owner_uid):
    repo = BriefingRepository(firestore_db)
    schedule = repo.create_schedule(owner_uid, _sched("crash-before-advance"), NOW)
    ref = firestore_db.collection("briefing_schedules").document(schedule["id"])
    ref.update({"next_run_at": NOW})
    claim = repo.reserve(owner_uid, schedule["id"], NOW)
    # Explicit emulator-only crash state: the unique stub exists, advancement did not commit.
    ref.update({"next_run_at": NOW})
    assert repo.reserve(owner_uid, schedule["id"], NOW) is None
    later = NOW + timedelta(minutes=11)
    service = BriefingService(
        firestore_db,
        _settings(),
        research_factory=lambda: FakeResearch(later),
        discord=FakeDiscord(),
        clock=lambda: later,
    )
    result = service.tick(owner_uid)
    assert result["generated"] == 1
    assert result["parts_sent"] >= 1
    assert repo.get(owner_uid, claim["id"])["delivery_status"] == "sent"
    assert ref.get().to_dict()["next_run_at"] > later
    assert len(repo.list(owner_uid)["items"]) == 1
    assert service.tick(owner_uid) == {"generated": 0, "parts_sent": 0, "delivery_errors": 0}


def test_concurrent_dispatch_has_one_part_owner(firestore_db, owner_uid):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    repo = BriefingRepository(firestore_db)
    preview = repo.save_preview(owner_uid, {"market": "KR", "instrument_ids": []}, _content())
    repo.queue(owner_uid, preview["id"], "dispatch-herd", ["one real delivery intent"], NOW)
    barrier = threading.Barrier(8)

    def claim(_):
        barrier.wait(timeout=30)
        return BriefingRepository(firestore_db).claim_part(owner_uid, preview["id"], NOW)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = [item for item in pool.map(claim, range(8)) if item]
    assert len(claims) == 1
    assert repo.get(owner_uid, preview["id"])["parts"][0]["attempts"] == 1
    assert repo.finish_part(claims[0], NOW, result={"message_id": "456"})
    assert repo.claim_part(owner_uid, preview["id"], NOW) is None
