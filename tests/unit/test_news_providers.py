"""News provider adapter tests (synthetic payloads only, no live calls)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.errors import AppError
from app.providers.news import (
    FinnhubNewsProvider,
    NaverNewsProvider,
    deduplicate_articles,
    normalize_text,
    normalize_url,
)

NOW = datetime(2026, 10, 22, 0, 0, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=7)


def _naver_settings(cid="test-id", sec="test-secret", mode="api_hub"):
    return SimpleNamespace(naver_client_id=cid, naver_client_secret=sec, naver_api_mode=mode)


def _finnhub_settings(tok="test-token"):
    return SimpleNamespace(finnhub_api_key=tok)


def _naver_item(title="T", origin="https://example.com/a", pub="Wed, 21 Oct 2026 09:00:00 +0900"):
    # Synthetic test payload only - mimics Naver search shape, not a live response.
    return {
        "title": title,
        "originallink": origin,
        "link": "https://search.naver.com/redirect",
        "description": "desc",
        "pubDate": pub,
    }


def _finnhub_item(i=1, ts=None, url=None):
    # Synthetic test payload only - mimics Finnhub JSON shape, not a live response.
    base = int(datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    return {
        "category": "general",
        "datetime": base if ts is None else ts,
        "headline": "Head " + str(i),
        "id": 1000 + i,
        "image": "",
        "related": "",
        "source": "TestSource",
        "summary": "Summary " + str(i),
        "url": "https://example.com/f" + str(i) if url is None else url,
    }


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_normalize_url_strips_fragment_and_tracking_only():
    url = "https://Example.COM:443/path?utm_source=naver&utm_medium=x&fbclid=AA&gclid=BB&id=123&foo=bar#frag"
    assert normalize_url(url) == "https://example.com/path?id=123&foo=bar"
    assert normalize_url("https://example.com/a?utm_term=x") == "https://example.com/a"
    assert normalize_url("http://example.com:80/a?x=1") == "http://example.com/a?x=1"
    assert normalize_url("ftp://example.com/a") is None
    assert normalize_url("https://user:pass@example.com/") is None
    assert normalize_url("https://user@example.com/") is None
    assert normalize_url("not a url") is None
    assert normalize_url("") is None
    assert normalize_url(None) is None


def test_normalize_text_drops_script_style_and_unescapes():
    raw = "<script>alert(1)</script><style>.x{}</style><b>Hi</b> &amp;  bye<br>  x"
    assert normalize_text(raw) == "Hi & bye x"
    assert normalize_text(None) == ""
    assert normalize_text(123) == ""


def test_deduplicate_prefers_newest_and_avoids_unrelated_merge():
    older = {
        "id": "a",
        "title": "Same Title",
        "source": "SrcA",
        "source_url": "https://example.com/a?utm_source=x",
        "published_at": datetime(2026, 10, 20, tzinfo=timezone.utc),
    }
    newer = {
        "id": "b",
        "title": "Same Title",
        "source": "SrcA",
        "source_url": "https://example.com/b",
        "published_at": datetime(2026, 10, 21, tzinfo=timezone.utc),
    }
    unrelated = {
        "id": "c",
        "title": "Same Title",
        "source": "Other",
        "source_url": "https://example.com/c",
        "published_at": datetime(2026, 10, 21, tzinfo=timezone.utc),
    }
    dup_url = {
        "id": "d",
        "title": "Different Title",
        "source": "SrcA",
        "source_url": "https://example.com/b#frag",
        "published_at": datetime(2026, 10, 19, tzinfo=timezone.utc),
    }
    out = deduplicate_articles([older, newer, unrelated, dup_url])
    urls = [r["source_url"] for r in out]
    # The same recurring headline on another day is a distinct article.
    assert len(out) == 3
    assert out[-1]["id"] == "a"
    assert out[0]["source_url"] in ("https://example.com/b", "https://example.com/c")
    assert urls.count("https://example.com/b") == 1
    # newest first, deterministic by id on ties
    assert out[0]["published_at"] >= out[1]["published_at"]


def test_naver_sends_headers_query_and_normalizes_rows():
    seen = {}

    def handler(request: httpx.Request):
        seen["headers"] = dict(request.headers)
        seen["params"] = dict(request.url.params)
        seen["url"] = str(request.url)
        payload = {
            "lastBuildDate": "Wed, 22 Oct 2026 00:00:00 +0900",
            "total": 2,
            "start": 1,
            "display": 100,
            "items": [
                _naver_item(
                    title="<b>Market</b> up &amp; down<script>bad()</script>",
                    origin="https://example.com/news/1?utm_source=naver&id=7#frag",
                ),
                _naver_item(title="Second", origin="https://example.org/s2"),
            ],
        }
        return httpx.Response(200, json=payload)

    p = NaverNewsProvider(_naver_settings(), client=_client(handler))
    res = p.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
    h = {k.lower(): v for k, v in seen["headers"].items()}
    assert h["x-ncp-apigw-api-key-id"] == "test-id"
    assert h["x-ncp-apigw-api-key"] == "test-secret"
    assert "x-naver-client-id" not in h
    assert "x-naver-client-secret" not in h
    assert seen["params"]["query"] == "증시"
    assert seen["params"]["sort"] == "date"
    assert seen["params"]["display"] == "100"
    assert seen["params"]["start"] == "1"
    assert seen["params"]["format"] == "json"
    assert seen["url"].startswith("https://naverapihub.apigw.ntruss.com/search/v1/news?")
    assert res["query"] == "증시"
    assert res["truncated"] is False
    assert "증시" in res["coverage_note"]
    assert len(res["items"]) == 2
    first = [a for a in res["items"] if "example.com" in a["source_url"]][0]
    assert first["title"] == "Market up & down"
    assert "<" not in first["title"] and "bad()" not in first["title"]
    assert first["source_url"] == "https://example.com/news/1?id=7"
    assert first["source"] == "example.com"
    assert first["provider"] == "naver"
    assert first["market"] == "KR"
    assert first["instrument_ids"] == []
    assert first["content_basis"] == "provider_title_and_summary"
    assert first["published_at"].tzinfo is not None
    assert first["fetched_at"] == NOW
    assert first["id"] == hashlib.sha256(first["source_url"].encode()).hexdigest()
    assert len(first["title"]) <= 500 and len(first["summary"]) <= 1500


def test_naver_legacy_credentials_use_only_legacy_endpoint():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    provider = NaverNewsProvider(_naver_settings(mode="legacy"), client=_client(handler))
    provider.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url).startswith("https://openapi.naver.com/v1/search/news.json?")
    assert request.headers["x-naver-client-id"] == "test-id"
    assert request.headers["x-naver-client-secret"] == "test-secret"
    assert "x-ncp-apigw-api-key" not in request.headers
    assert "x-ncp-apigw-api-key-id" not in request.headers
    assert "format" not in request.url.params


@pytest.mark.parametrize("mode", ["api_hub", "legacy"])
def test_naver_auth_failure_does_not_retry_against_other_endpoint(mode):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(403)

    provider = NaverNewsProvider(_naver_settings(mode=mode), client=_client(handler))
    with pytest.raises(AppError) as error:
        provider.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
    assert error.value.code == "news_provider_unauthorized"
    assert len(seen) == 1


def test_naver_invalid_mode_fails_before_http_request():
    def handler(request: httpx.Request):
        pytest.fail("Invalid mode must not send credentials")

    provider = NaverNewsProvider(_naver_settings(mode="invalid"), client=_client(handler))
    with pytest.raises(AppError) as error:
        provider.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
    assert error.value.code == "news_not_configured"


def test_naver_instrument_query_catalog_mapping():
    cases = [
        ({"id": "INDEX_KOSPI", "market": "KR"}, "코스피"),
        ({"id": "INDEX_KOSDAQ", "market": "KR"}, "코스닥"),
        ({"id": "KRX_005930", "market": "KR"}, "삼성전자"),
        ({"id": "KRX_000660", "market": "KR"}, "SK하이닉스"),
    ]
    for inst, expected in cases:
        got = {}

        def handler(request: httpx.Request, _got=got):
            _got["q"] = dict(request.url.params).get("query")
            return httpx.Response(200, json={"total": 0, "start": 1, "display": 100, "items": []})

        p = NaverNewsProvider(_naver_settings(), client=_client(handler))
        res = p.fetch(market="KR", instrument=dict(inst), since=SINCE, now=NOW)
        assert got["q"] == expected
        assert res["query"] == expected
        assert res["items"][0:0] == []
        # instrument id is attached when rows exist; empty here so just query check

    # instrument_ids attached on rows
    def handler2(request: httpx.Request):
        return httpx.Response(
            200, json={"total": 1, "start": 1, "display": 100, "items": [_naver_item()]}
        )

    p2 = NaverNewsProvider(_naver_settings(), client=_client(handler2))
    res2 = p2.fetch(
        market="KR", instrument={"id": "KRX_005930", "market": "KR"}, since=SINCE, now=NOW
    )
    assert res2["items"][0]["instrument_ids"] == ["KRX_005930"]


def test_naver_pagination_truncated_after_five_pages():
    calls = []

    def handler(request: httpx.Request):
        calls.append(int(dict(request.url.params)["start"]))
        n = len(calls)
        items = [
            _naver_item(
                title="T" + str(n) + "-" + str(i),
                origin="https://example.com/p" + str(n) + "/" + str(i),
            )
            for i in range(100)
        ]
        return httpx.Response(
            200, json={"total": 1000, "start": calls[-1], "display": 100, "items": items}
        )

    p = NaverNewsProvider(_naver_settings(), client=_client(handler))
    res = p.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
    assert calls == [1, 101, 201, 301, 401]
    assert res["truncated"] is True
    assert "truncated" in res["coverage_note"].lower()
    assert len(res["items"]) == 500


def test_naver_breaks_when_page_gets_old_and_filters_window():
    calls = []

    def handler(request: httpx.Request):
        calls.append(int(dict(request.url.params)["start"]))
        if len(calls) == 1:
            items = [
                _naver_item(title="New", origin="https://example.com/new"),
                _naver_item(
                    title="Old",
                    origin="https://example.com/old",
                    pub="Wed, 01 Oct 2026 09:00:00 +0900",
                ),
            ]
        else:
            items = [_naver_item(title="Older", origin="https://example.com/older")]
        return httpx.Response(
            200, json={"total": 500, "start": calls[-1], "display": 100, "items": items}
        )

    p = NaverNewsProvider(_naver_settings(), client=_client(handler))
    res = p.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
    assert len(calls) == 1
    assert [a["title"] for a in res["items"]] == ["New"]
    assert res["truncated"] is False


def test_naver_skips_malformed_future_and_out_of_window_rows():
    future_ts = NOW + timedelta(minutes=30)
    future_pub = future_ts.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = [
        _naver_item(title="", origin="https://example.com/empty-title"),
        _naver_item(title="BadURL", origin="ftp://example.com/x"),
        _naver_item(title="BadDate", origin="https://example.com/baddate", pub="not-a-date"),
        _naver_item(title="Future", origin="https://example.com/future", pub=future_pub),
        _naver_item(
            title="TooOld", origin="https://example.com/old", pub="Wed, 01 Oct 2026 09:00:00 +0900"
        ),
        _naver_item(title="Good", origin="https://example.com/good"),
    ]

    def handler(request: httpx.Request):
        return httpx.Response(200, json={"total": 6, "start": 1, "display": 100, "items": items})

    p = NaverNewsProvider(_naver_settings(), client=_client(handler))
    res = p.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
    assert [a["title"] for a in res["items"]] == ["Good"]


def test_naver_missing_credentials_safe_message():
    for bad in (
        SimpleNamespace(naver_client_id=None, naver_client_secret=None),
        SimpleNamespace(naver_client_id="  ", naver_client_secret="x"),
        SimpleNamespace(naver_client_id="x", naver_client_secret=""),
    ):
        p = NaverNewsProvider(bad, client=_client(lambda r: httpx.Response(200, json={})))
        with pytest.raises(AppError) as e:
            p.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
        assert e.value.status_code == 503
        assert e.value.code == "news_not_configured"
        assert "NAVER_CLIENT_ID" in e.value.message
        assert "NAVER_CLIENT_SECRET" in e.value.message


def test_naver_market_mismatch_rejected():
    p = NaverNewsProvider(_naver_settings(), client=_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(AppError) as e:
        p.fetch(market="US", instrument=None, since=SINCE, now=NOW)
    assert e.value.code == "news_market_not_supported"


def test_naver_error_mapping_is_credential_safe():
    secret = "super-secret-value"
    cases = [
        (401, 502, "news_provider_unauthorized"),
        (403, 502, "news_provider_unauthorized"),
        (429, 503, "news_rate_limited"),
        (500, 503, "news_provider_unavailable"),
        (503, 503, "news_provider_unavailable"),
    ]
    for status, exp_status, exp_code in cases:

        def handler(request: httpx.Request, _s=status):
            return httpx.Response(_s, text="leaky-body-should-not-surface")

        p = NaverNewsProvider(_naver_settings(sec=secret), client=_client(handler))
        with pytest.raises(AppError) as e:
            p.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
        assert e.value.status_code == exp_status
        assert e.value.code == exp_code
        assert secret not in e.value.message
        assert "leaky-body" not in e.value.message
        assert "openapi.naver.com" not in e.value.message


def test_naver_invalid_json_and_bad_payload():
    def bad_json(request: httpx.Request):
        return httpx.Response(200, content=b"not-json{")

    p = NaverNewsProvider(_naver_settings(), client=_client(bad_json))
    with pytest.raises(AppError) as e:
        p.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
    assert e.value.code == "news_payload_invalid"

    for payload in ({"no": "items"}, {"items": "not-a-list"}, ["list-not-dict"]):

        def handler(request: httpx.Request, _p=payload):
            return httpx.Response(200, json=_p)

        p2 = NaverNewsProvider(_naver_settings(), client=_client(handler))
        with pytest.raises(AppError) as e2:
            p2.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
        assert e2.value.code == "news_payload_invalid"


def test_finnhub_general_sends_header_category_and_no_token_in_url():
    seen = {}

    def handler(request: httpx.Request):
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["params"] = dict(request.url.params)
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[_finnhub_item(1), _finnhub_item(2)])

    p = FinnhubNewsProvider(_finnhub_settings(), client=_client(handler))
    res = p.fetch(market="US", instrument=None, since=SINCE, now=NOW)
    assert seen["headers"]["x-finnhub-token"] == "test-token"
    assert seen["params"] == {"category": "general"}
    assert "test-token" not in seen["url"]
    assert "token" not in seen["params"]
    assert str(request_url_path(seen["url"])) == "/api/v1/news"
    assert res["query"] == "general"
    assert res["truncated"] is False
    assert len(res["items"]) == 2
    first = res["items"][0]
    assert first["provider"] == "finnhub"
    assert first["market"] == "US"
    assert first["instrument_ids"] == []
    assert first["source"] == "TestSource"
    assert first["fetched_at"] == NOW
    assert first["content_basis"] == "provider_title_and_summary"
    assert first["id"] == hashlib.sha256(first["source_url"].encode()).hexdigest()


def request_url_path(url: str) -> str:
    return httpx.URL(url).path


def test_finnhub_company_news_window_params_and_query():
    seen = {}

    def handler(request: httpx.Request):
        seen["params"] = dict(request.url.params)
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[_finnhub_item(1)])

    inst = {"id": "NASDAQ_AAPL", "market": "US", "yahoo_symbol": "AAPL"}
    p = FinnhubNewsProvider(_finnhub_settings(), client=_client(handler))
    res = p.fetch(market="US", instrument=inst, since=SINCE, now=NOW)
    assert seen["params"]["symbol"] == "AAPL"
    assert seen["params"]["from"] == SINCE.date().isoformat()
    assert seen["params"]["to"] == NOW.date().isoformat()
    assert "test-token" not in seen["url"]
    assert res["query"] == "AAPL"
    assert res["items"][0]["instrument_ids"] == ["NASDAQ_AAPL"]
    assert res["items"][0]["provider_id"] == "1001"


def test_finnhub_index_uses_general_category():
    seen = {}

    def handler(request: httpx.Request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    p = FinnhubNewsProvider(_finnhub_settings(), client=_client(handler))
    res = p.fetch(
        market="US", instrument={"id": "INDEX_SP500", "market": "US"}, since=SINCE, now=NOW
    )
    assert seen["params"] == {"category": "general"}
    assert res["query"] == "general"
    assert res["items"] == []


def test_finnhub_filters_and_skips_malformed_rows():
    base = int(datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc).timestamp())
    old = int((SINCE - timedelta(days=1)).timestamp())
    future = int((NOW + timedelta(minutes=30)).timestamp())
    payload = [
        _finnhub_item(1, ts=base),
        _finnhub_item(2, ts=old),
        _finnhub_item(3, ts=future),
        dict(_finnhub_item(4, ts=base), headline="   "),
        dict(_finnhub_item(5, ts=base), url="ftp://example.com/x"),
        dict(_finnhub_item(6, ts=base), datetime="not-a-number"),
        "not-a-dict",
    ]

    def handler(request: httpx.Request):
        return httpx.Response(200, json=payload)

    p = FinnhubNewsProvider(_finnhub_settings(), client=_client(handler))
    res = p.fetch(market="US", instrument=None, since=SINCE, now=NOW)
    assert [a["title"] for a in res["items"]] == ["Head 1"]
    # non-finite unix timestamps are rejected without transport
    assert (
        FinnhubNewsProvider._normalize_row(
            dict(_finnhub_item(9, ts=base), datetime=float("inf")), "US", [], NOW
        )
        is None
    )
    assert (
        FinnhubNewsProvider._normalize_row(
            dict(_finnhub_item(9, ts=base), datetime=float("nan")), "US", [], NOW
        )
        is None
    )


def test_finnhub_missing_key_market_mismatch_and_errors_credential_safe():
    p = FinnhubNewsProvider(
        SimpleNamespace(finnhub_api_key=None),
        client=_client(lambda r: httpx.Response(200, json=[])),
    )
    with pytest.raises(AppError) as e:
        p.fetch(market="US", instrument=None, since=SINCE, now=NOW)
    assert (e.value.status_code, e.value.code) == (503, "news_not_configured")
    assert "FINNHUB_API_KEY" in e.value.message

    p2 = FinnhubNewsProvider(
        _finnhub_settings(), client=_client(lambda r: httpx.Response(200, json=[]))
    )
    with pytest.raises(AppError) as e2:
        p2.fetch(market="KR", instrument=None, since=SINCE, now=NOW)
    assert e2.value.code == "news_market_not_supported"

    for status, exp_status, exp_code in [
        (401, 502, "news_provider_unauthorized"),
        (429, 503, "news_rate_limited"),
        (500, 503, "news_provider_unavailable"),
    ]:

        def handler(request: httpx.Request, _s=status):
            return httpx.Response(_s, text="secret-body")

        pp = FinnhubNewsProvider(_finnhub_settings("tok123"), client=_client(handler))
        with pytest.raises(AppError) as ee:
            pp.fetch(market="US", instrument=None, since=SINCE, now=NOW)
        assert (ee.value.status_code, ee.value.code) == (exp_status, exp_code)
        assert "tok123" not in ee.value.message
        assert "secret-body" not in ee.value.message

    def nonlist(request: httpx.Request):
        return httpx.Response(200, json={"articles": []})

    with pytest.raises(AppError) as ne:
        FinnhubNewsProvider(_finnhub_settings(), client=_client(nonlist)).fetch(
            market="US", instrument=None, since=SINCE, now=NOW
        )
    assert ne.value.code == "news_payload_invalid"


@pytest.mark.parametrize(
    "provider,payload",
    [
        (NaverNewsProvider, {"items": [{"changed_contract": True}]}),
        (FinnhubNewsProvider, [{"changed_contract": True}]),
    ],
)
def test_nonempty_but_all_invalid_payload_is_not_reported_as_no_news(provider, payload):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        adapter = provider(
            _naver_settings() if provider is NaverNewsProvider else _finnhub_settings(),
            client=client,
        )
        with pytest.raises(AppError) as error:
            adapter.fetch(
                market="KR" if provider is NaverNewsProvider else "US",
                instrument=None,
                since=SINCE,
                now=NOW,
            )
        assert error.value.code == "news_payload_invalid"
