"""Firestore-emulator integration tests for research API/services/repository.

Scope: cache TTL/stale + cooldown, missing news key, original-only separation,
rolling news windows, partial overview + auth/validation, repository lease fence.
Uses explicit provider doubles, real Firestore emulator, frozen NOW (2025-10-24),
no network. Touches live VIX/CNN cache keys in overview test; main must reseed
real cache after the suite (SP500/VIX/CNN).
"""

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_current_uid, get_db
from app.errors import AppError
from app.main import app
from app.repositories.research import ResearchRepository
from app.services.research import ResearchService

pytestmark = pytest.mark.integration

NOW = datetime(2025, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
ANALYSIS_KEY_AAPL = "analysis:NASDAQ_AAPL:1y"
ANALYSIS_KEY_VIX = "analysis:INDEX_VIX:1y"
NEWS_KEY_AAPL_24H = "news:US:NASDAQ_AAPL:24h"
SENTIMENT_KEY = "sentiment:US:cnn"


def _settings(**over):
    base = dict(
        analysis_cache_minutes=30,
        news_cache_minutes=30,
        sentiment_cache_minutes=30,
        openai_api_key=None,
        openai_model=None,
        naver_client_id="test-id",
        naver_client_secret="test-secret",
        finnhub_api_key="test-key",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _fixture_bars():
    raw = json.loads((Path(__file__).parents[1] / "fixtures" / "yahoo_aapl_daily.json").read_text())
    bars = raw["bars"]
    for bar in bars:
        for key in ("event_time", "fetched_at"):
            bar[key] = datetime.fromisoformat(bar[key])
    return copy.deepcopy(bars)


class FakeYahoo:
    def __init__(self, bars=None, exc=None):
        self._bars = bars
        self._exc = exc
        self.calls = 0

    def fetch_daily(self, instrument, start_date, end_date):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return copy.deepcopy(self._bars)


class ExplodingProvider:
    def fetch(self, *args, **kwargs):
        raise AssertionError("upstream must not be called")


class FakeCNN:
    def __init__(self, exc=None, value=55.0):
        self._exc = exc
        self._value = value
        self.calls = 0

    def fetch(self, *, now):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return {
            "provider": "cnn",
            "market": "US",
            "value": self._value,
            "rating": "Neutral",
            "event_time": now,
            "fetched_at": now,
            "source_url": "https://www.cnn.com/markets/fear-and-greed",
            "endpoint_url": "https://example.com/cnn",
            "previous_close": None,
            "previous_1_week": None,
            "previous_1_month": None,
            "previous_1_year": None,
            "history": [],
            "notes": [],
        }


def _news_item(now, idx, token, market="US", instrument_ids=None, age=None):
    pub = now - age
    return {
        "id": f"test-{token}-{idx}",
        "provider": "finnhub",
        "provider_id": f"https://example.com/test/{token}/{idx}",
        "market": market,
        "instrument_ids": list(instrument_ids or []),
        "title": f"Test headline {token} {idx}",
        "summary": f"Test summary {token} {idx}",
        "source": "example.com",
        "source_url": f"https://example.com/test/{token}/{idx}",
        "published_at": pub,
        "fetched_at": now,
        "content_basis": "provider_title_and_summary",
    }


class FakeNews:
    def __init__(self, items=None, exc=None):
        self._items = items or []
        self._exc = exc
        self.calls = 0

    def fetch(self, *, market, instrument, since, now):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return {"items": copy.deepcopy(self._items), "truncated": False, "coverage_note": "test"}


@pytest.fixture
def client(firestore_db, owner_uid):
    app.dependency_overrides[get_db] = lambda: firestore_db
    app.dependency_overrides[get_current_uid] = lambda: owner_uid
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_research_cache(firestore_db):
    keys = (
        ANALYSIS_KEY_AAPL,
        ANALYSIS_KEY_VIX,
        NEWS_KEY_AAPL_24H,
        SENTIMENT_KEY,
        "news:US:market:24h",
    )
    repo = ResearchRepository(firestore_db)
    for key in keys:
        repo.ref(key).delete()
    yield
    for key in keys:
        repo.ref(key).delete()
    from google.cloud.firestore_v1.base_query import FieldFilter

    for doc in (
        firestore_db.collection("news_articles")
        .where(filter=FieldFilter("source", "==", "example.com"))
        .stream()
    ):
        doc.reference.delete()
    firestore_db.collection("sentiment").document("US_CNN_FNG_2025-10-24").delete()


def _svc(db, now=NOW, settings=None, yahoo=None, cnn=None, naver=None, finnhub=None):
    kw = {}
    if yahoo is not None:
        kw["yahoo"] = yahoo
    if cnn is not None:
        kw["cnn"] = cnn
    if naver is not None:
        kw["naver"] = naver
    if finnhub is not None:
        kw["finnhub"] = finnhub
    return ResearchService(db, settings or _settings(), now=now, **kw)


def test_analysis_cache_ttl_stale_and_retry_cooldown(firestore_db):
    bars = _fixture_bars()
    yahoo = FakeYahoo(bars=bars)
    svc = _svc(
        firestore_db,
        yahoo=yahoo,
        cnn=ExplodingProvider(),
        naver=ExplodingProvider(),
        finnhub=ExplodingProvider(),
    )
    first = svc.analysis("NASDAQ_AAPL")
    assert first["observation_count"] == 3, first
    assert yahoo.calls == 1
    second = svc.analysis("NASDAQ_AAPL")
    assert yahoo.calls == 1
    assert second["fetched_at"] == first["fetched_at"]
    failing = AppError(503, "provider_unavailable", "down")
    later = NOW + timedelta(minutes=31)
    svc_fail = _svc(
        firestore_db,
        now=later,
        yahoo=FakeYahoo(exc=failing),
        cnn=ExplodingProvider(),
        naver=ExplodingProvider(),
        finnhub=ExplodingProvider(),
    )
    stale = svc_fail.analysis("NASDAQ_AAPL")
    assert stale["observation_count"] == 3, stale
    assert stale["quality_status"] == "stale", stale
    assert stale["fetched_at"] == first["fetched_at"]
    assert (stale["error"] or {}).get("code") == "provider_unavailable"
    yahoo2 = FakeYahoo(exc=failing)
    svc_cool = _svc(
        firestore_db,
        now=later + timedelta(minutes=1),
        yahoo=yahoo2,
        cnn=ExplodingProvider(),
        naver=ExplodingProvider(),
        finnhub=ExplodingProvider(),
    )
    again = svc_cool.analysis("NASDAQ_AAPL")
    assert yahoo2.calls == 0
    assert again["observation_count"] == 3
    assert again["quality_status"] == "stale"


def test_no_key_news_unavailable_not_fake_empty(firestore_db):
    svc = ResearchService(
        firestore_db,
        _settings(naver_client_id=None, naver_client_secret=None, finnhub_api_key=None),
        now=NOW,
    )
    res = svc.news("US", None, "24h")
    assert res["quality_status"] == "unavailable", res
    assert res["items"] == []
    assert (res["error"] or {}).get("code") == "news_not_configured", res
    assert res.get("fetched_at") is None


def test_original_only_save_never_mutates_user_data(client, firestore_db, owner_uid):
    body = {
        "instrument_id": "NASDAQ_AAPL",
        "date": "2025-10-22",
        "value": 999,
        "memo": "user edit",
        "idempotency_key": "research-orig-" + uuid4().hex,
    }
    created = client.post("/api/data", json=body)
    assert created.status_code == 201, created.text
    bars = _fixture_bars()
    svc = _svc(
        firestore_db,
        yahoo=FakeYahoo(bars=bars),
        cnn=ExplodingProvider(),
        naver=ExplodingProvider(),
        finnhub=ExplodingProvider(),
    )
    result = svc.analysis("NASDAQ_AAPL")
    assert result["observation_count"] == 3
    got = client.get(
        "/api/data",
        params={
            "instrument_id": "NASDAQ_AAPL",
            "start_date": "2025-10-21",
            "end_date": "2025-10-23",
        },
    ).json()
    mine = [r for r in got["items"] if r["date"] == "2025-10-22" and r.get("memo") == "user edit"]
    assert mine and mine[0]["value"] == 999, got
    assert len(got["items"]) == 1, got
    from app.repositories.market import MarketRepository

    hist = MarketRepository(firestore_db).history("NASDAQ_AAPL", "2025-10-21", "2025-10-23")
    assert hist[0]["adjusted_close"] == bars[0]["adjusted_close"]


def test_rolling_news_window_filters_expired_and_summary(firestore_db):
    token = uuid4().hex[:8]
    fresh_age = timedelta(hours=1)
    old_age = timedelta(hours=23, minutes=50)
    seed_items = [
        _news_item(NOW, 0, token, instrument_ids=["NASDAQ_AAPL"], age=fresh_age),
        _news_item(NOW, 1, token, instrument_ids=["NASDAQ_AAPL"], age=old_age),
    ]
    seeder = _svc(
        firestore_db,
        finnhub=FakeNews(items=seed_items),
        naver=ExplodingProvider(),
        cnn=ExplodingProvider(),
        yahoo=ExplodingProvider(),
    )
    seeded = seeder.news("US", "NASDAQ_AAPL", "24h")
    assert len(seeded["items"]) == 2, seeded
    reader = _svc(
        firestore_db,
        now=NOW + timedelta(minutes=20),
        finnhub=FakeNews(exc=AppError(503, "x", "down")),
        naver=ExplodingProvider(),
        cnn=ExplodingProvider(),
        yahoo=ExplodingProvider(),
    )
    res = reader.news("US", "NASDAQ_AAPL", "24h", refresh=False)
    assert [i["id"] for i in res["items"]] == [f"test-{token}-0"], res
    assert res["summary"]["method"] == "extractive"
    assert len(res["summary"]["highlights"]) == 1
    assert res["event_time"] == seed_items[0]["published_at"]


def test_overview_partial_cnn_failure_vix_ok_and_api_auth(client, firestore_db):
    bars = _fixture_bars()
    yahoo = FakeYahoo(bars=bars)
    finnhub = FakeNews(
        items=[
            _news_item(
                NOW, 0, uuid4().hex[:8], instrument_ids=["NASDAQ_AAPL"], age=timedelta(hours=1)
            )
        ]
    )
    svc = _svc(
        firestore_db,
        yahoo=yahoo,
        cnn=FakeCNN(exc=AppError(503, "cnn_unavailable", "cnn down")),
        naver=ExplodingProvider(),
        finnhub=finnhub,
    )
    # Explicit test-only VIX cache, not a relabeled real AAPL observation.
    repo = ResearchRepository(firestore_db)
    _, token = repo.acquire(ANALYSIS_KEY_VIX, NOW, refresh=True)
    repo.finish(
        ANALYSIS_KEY_VIX,
        token,
        NOW,
        30,
        payload={
            "instrument_id": "INDEX_VIX",
            "quality_status": "ok",
            "observation_count": 61,
            "latest_close": 18.5,
            "end_date": "2025-10-23",
            "data_as_of": NOW,
            "source_urls": ["https://example.test/vix"],
        },
    )
    ov = svc.overview("NASDAQ_AAPL", "24h")
    assert (ov["sentiment"]["cnn"].get("error") or {}).get("code") == "cnn_unavailable", ov[
        "sentiment"
    ]["cnn"]
    assert ov["sentiment"]["cnn"]["quality_status"] in ("stale", "unavailable"), ov["sentiment"][
        "cnn"
    ]
    assert ov["sentiment"]["vix"]["latest_close"] == 18.5
    assert ov["sentiment"]["vix"]["quality_status"] == "ok"
    assert ov["analysis"]["observation_count"] == 3
    assert ov["news"]["items"], ov["news"]
    auth_override = app.dependency_overrides.pop(get_current_uid)
    with TestClient(app) as anon:
        app.dependency_overrides[get_db] = lambda: firestore_db
        try:
            r = anon.get("/api/news", params={"market": "US"})
            assert r.status_code == 401, r.text
        finally:
            app.dependency_overrides[get_current_uid] = auth_override
    assert client.get("/api/news", params={"market": "XX"}).status_code == 422
    bad = client.get("/api/news", params={"market": "US", "instrument_id": "KRX_005930"})
    assert bad.status_code == 422, bad.text
    assert bad.json()["code"] == "market_mismatch"


def test_repository_lease_fence_old_token_cannot_overwrite(firestore_db):
    repo = ResearchRepository(firestore_db)
    key = "test-lease-" + uuid4().hex
    now = NOW
    state, token = repo.acquire(key, now, refresh=True)
    assert token
    assert repo.acquire(key, now + timedelta(seconds=1), refresh=True)[1] is None
    repo.ref(key).set(
        {"attempt_token": "newer-token", "lease_until": now + timedelta(minutes=3)}, merge=True
    )
    after = repo.finish(key, token, now, 30, payload={"observation_count": 1})
    assert after.get("payload") is None, after
    committed = repo.finish(key, "newer-token", now, 30, payload={"observation_count": 7})
    assert committed["payload"]["observation_count"] == 7
    assert repo.get(key)["payload"]["observation_count"] == 7
    repo.ref(key).delete()


def test_research_routes_cached_chat_context_and_persisted_sources(
    client, firestore_db, owner_uid, monkeypatch
):
    from app.schemas.conversation import ChatRequest
    from app.services.chat import handle_chat

    repo = ResearchRepository(firestore_db)
    _, token = repo.acquire(ANALYSIS_KEY_VIX, NOW, refresh=True)
    repo.finish(
        ANALYSIS_KEY_VIX,
        token,
        NOW,
        30,
        payload={
            "instrument_id": "INDEX_VIX",
            "observation_count": 61,
            "latest_close": 18.5,
            "quality_status": "ok",
            "end_date": "2025-10-23",
            "source_urls": ["https://example.test/vix"],
        },
    )
    item = _news_item(
        NOW, 0, uuid4().hex[:8], instrument_ids=["NASDAQ_AAPL"], age=timedelta(hours=1)
    )
    yahoo, cnn, news = FakeYahoo(_fixture_bars()), FakeCNN(value=0), FakeNews([item])
    svc = _svc(firestore_db, yahoo=yahoo, cnn=cnn, finnhub=news)
    monkeypatch.setattr("app.api.research.ResearchService", lambda db: svc)
    analysis = client.get("/api/analysis/NASDAQ_AAPL?period=1y")
    assert analysis.status_code == 200, analysis.text
    assert analysis.json()["observation_count"] == 3
    assert client.get("/api/analysis/NASDAQ_AAPL?period=1d").status_code == 422
    sentiment = client.get("/api/sentiment/us")
    assert sentiment.status_code == 200, sentiment.text
    assert sentiment.json()["cnn"]["value"] == 0
    articles = client.get("/api/news?market=US&instrument_id=NASDAQ_AAPL&window=24h")
    assert articles.status_code == 200, articles.text
    assert articles.json()["items"][0]["source_url"] == item["source_url"]
    overview = client.get("/api/market/overview?instrument_id=NASDAQ_AAPL")
    assert overview.status_code == 200, overview.text
    counts = yahoo.calls, cnn.calls, news.calls
    context, sources = svc.chat_context("NASDAQ_AAPL")
    assert (yahoo.calls, cnn.calls, news.calls) == counts
    assert context["news"]["items"][0]["title"] == item["title"]
    assert "history" not in context["cnn"]
    assert any(source["url"] == item["source_url"] for source in sources)

    def model(**kwargs):
        assert item["title"] in kwargs["instructions"]
        assert '"value": 0' in kwargs["instructions"]
        return {
            "text": "테스트 전용 모델 응답",
            "sources": [{"title": "tool source", "url": "https://example.test/tool"}],
            "tool_trace": [{"tool": "search_news", "ok": True, "summary": "test-only"}],
        }

    response = handle_chat(
        firestore_db,
        owner_uid=owner_uid,
        payload=ChatRequest(
            message="뉴스와 지표를 설명해줘", client_request_id=uuid4(), instrument_id="NASDAQ_AAPL"
        ),
        settings=_settings(openai_api_key="test-only", openai_model="test-only"),
        summary_fn=lambda *_: {"research": context, "sources": sources},
        model_caller=model,
    )
    urls = {source.url for source in response.sources}
    assert item["source_url"] in urls
    assert "https://example.test/tool" in urls
    messages = [
        row.to_dict()
        for row in firestore_db.collection("conversations")
        .document(str(response.conversation_id))
        .collection("messages")
        .stream()
    ]
    saved = next(row for row in messages if row["role"] == "assistant")
    assert {source["url"] for source in saved["sources"]} == urls
    assert saved["tool_trace"][0]["tool"] == "search_news"

    detail = client.get(f"/api/conversations/{response.conversation_id}")
    assert detail.status_code == 200, detail.text
    restored = next(row for row in detail.json()["messages"] if row["role"] == "assistant")
    assert {source["url"] for source in restored["sources"]} == urls
    assert restored["tool_trace"][0]["tool"] == "search_news"
