"""Explicit unit doubles exercise partial digests; runtime always uses providers."""

from datetime import datetime, timedelta, timezone

from app.errors import AppError
from app.services.briefing_content import build_content

NOW = datetime(2026, 10, 21, 23, tzinfo=timezone.utc)


class PartialResearch:
    now = NOW

    def analysis(self, instrument_id):
        if instrument_id == "INDEX_NASDAQ":
            raise AppError(503, "calendar_unavailable", "거래소 달력을 확인할 수 없습니다.")
        return {
            "quality_status": "stale",
            "latest_close": 123,
            "returns_pct": {"1": 0},
            "data_as_of": NOW - timedelta(days=1),
            "source_urls": ["https://finance.yahoo.com/"],
        }

    def cnn_sentiment(self):
        return {
            "quality_status": "ok",
            "value": 0,
            "rating": "extreme fear",
            "event_time": NOW,
            "source_url": "https://www.cnn.com/markets/fear-and-greed",
        }

    def news(self, market, instrument_id, window, *, use_model):
        assert use_model is False
        return {
            "quality_status": "unavailable",
            "error": {"code": "news_not_configured", "message": "뉴스 키가 없습니다."},
        }


def test_partial_actual_components_remain_visible_and_zero_is_not_missing():
    result = build_content(PartialResearch(), {"market": "US", "instrument_ids": []})
    assert "123.00" in result["text"]
    assert "공포탐욕지수: 0.00 / extreme fear" in result["text"]
    assert "거래소 달력을 확인할 수 없습니다" in result["text"]
    assert "기사를 확인할 수 없습니다" in result["text"]
    assert result["sources"] and result["data_as_of"]
    assert result["warnings"]


def test_news_since_last_delivery_is_capped_and_filters_older_items():
    class NewsResearch(PartialResearch):
        def news(self, market, instrument_id, window, *, use_model):
            assert window == "7d"
            return {
                "quality_status": "ok",
                "items": [
                    {
                        "title": "old article",
                        "summary": "",
                        "published_at": NOW - timedelta(days=8),
                        "source_url": "https://example.test/old",
                    },
                    {
                        "title": "new article",
                        "summary": "observed abstract",
                        "published_at": NOW - timedelta(days=1),
                        "source_url": "https://example.test/new",
                    },
                ],
            }

    result = build_content(
        NewsResearch(),
        {"market": "KR", "instrument_ids": ["KRX_005930"]},
        since=NOW - timedelta(days=10),
    )
    assert "old article" not in result["text"]
    assert "new article" in result["text"]
    assert "7일" in result["warnings"][0]
    assert "CNN" not in result["text"]


def test_vix_is_labeled_as_expected_volatility_not_volatility_of_vix():
    research = PartialResearch()
    result = build_content(research, {"market": "US", "instrument_ids": ["INDEX_VIX"]})
    assert result["text"].count("VIX(미국 기대 변동성):") == 1
    vix_line = next(
        line for line in result["text"].splitlines() if line.startswith("VIX(미국 기대 변동성):")
    )
    assert "20일 연율 변동성" not in vix_line
    assert "백분위" in vix_line
    assert "UTC" in vix_line


def test_provider_source_url_is_preserved_without_truncating_it():
    url = "http://example.test/article/" + "a" * 1200

    class WithNews(PartialResearch):
        def news(self, *args, **kwargs):
            return {
                "quality_status": "ok",
                "items": [
                    {
                        "title": "Real provider shape",
                        "summary": "Abstract only",
                        "published_at": NOW,
                        "source_url": url,
                    }
                ],
            }

    result = build_content(WithNews(), {"market": "KR", "instrument_ids": []})
    assert url in [item["url"] for item in result["sources"]]
    assert url in result["text"]
