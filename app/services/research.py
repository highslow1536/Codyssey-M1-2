"""Actual providers, durable cache, and explicit stale/unavailable results."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.catalog import get_instrument
from app.config import get_settings
from app.domain.indicators import calculate_indicators
from app.domain.sessions import complete_sessions
from app.errors import AppError
from app.providers.cnn import CNNProvider
from app.providers.news import FinnhubNewsProvider, NaverNewsProvider, deduplicate_articles
from app.providers.yahoo import YahooProvider
from app.repositories.market import MarketRepository
from app.repositories.research import ResearchRepository
from app.services.news_summary import summarize_news


def utc_datetime(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(timezone.utc) if isinstance(value, datetime) and value.tzinfo else None


class ResearchService:
    def __init__(
        self, db, settings=None, *, now=None, yahoo=None, cnn=None, naver=None, finnhub=None
    ):
        self.db = db
        self.settings = settings or get_settings()
        self.now = now or datetime.now(timezone.utc)
        self.repository = ResearchRepository(db)
        self.market = MarketRepository(db)
        self.yahoo = yahoo or YahooProvider()
        self.cnn = cnn or CNNProvider()
        self.naver = naver or NaverNewsProvider(self.settings)
        self.finnhub = finnhub or FinnhubNewsProvider(self.settings)

    def _cached(self, key, collect, ttl, *, refresh=True):
        state, token = self.repository.acquire(key, self.now, refresh=refresh)
        if token:
            try:
                payload = collect()
                state = self.repository.finish(key, token, self.now, ttl, payload=payload)
            except AppError as exc:
                state = self.repository.finish(
                    key,
                    token,
                    self.now,
                    min(ttl, 5),
                    error={"code": exc.code, "message": exc.message},
                )
            except Exception:
                # Never return an upstream URL, credential, response body or arbitrary exception.
                state = self.repository.finish(
                    key,
                    token,
                    self.now,
                    min(ttl, 5),
                    error={
                        "code": "research_unavailable",
                        "message": "실제 데이터를 수집하지 못했습니다. 잠시 후 다시 확인하세요.",
                    },
                )
        result = dict(state.get("payload") or {})
        expired = not state.get("expires_at") or state["expires_at"] <= self.now
        result.update(
            quality_status=(
                "stale" if expired or state.get("error") else result.get("quality_status", "ok")
            )
            if result
            else "unavailable",
            error=state.get("error"),
            fetched_at=state.get("fetched_at"),
            cache_expires_at=state.get("expires_at"),
            last_attempt_at=state.get("last_attempt_at"),
            next_attempt_at=state.get("next_attempt_at"),
        )
        if not result.get("fetched_at") and not result["error"]:
            result["error"] = {
                "code": "not_collected",
                "message": "아직 수집된 데이터가 없습니다. 시장 브리핑에서 새로 확인하세요.",
            }
        return result

    def analysis(self, instrument_id, *, refresh=True):
        instrument = get_instrument(instrument_id)
        today = self.now.astimezone(ZoneInfo(instrument["timezone"])).date()
        start = today - timedelta(days=365)

        def collect():
            bars = self.yahoo.fetch_daily(instrument, start, today)
            self.market.save_original_bars(instrument, bars)
            result = calculate_indicators(bars, instrument)
            return self._analysis_freshness(result, instrument, start, today)

        result = self._cached(
            f"analysis:{instrument_id}:1y",
            collect,
            self.settings.analysis_cache_minutes,
            refresh=refresh,
        )
        # A normal data import can supply originals before research has ever been refreshed.
        if not result.get("observation_count"):
            bars = self.market.history(instrument_id, start, today)
            if bars:
                base = calculate_indicators(bars, instrument)
                base = self._analysis_freshness(base, instrument, start, today)
                result = {
                    **base,
                    **{
                        k: v for k, v in result.items() if k not in {"quality_status", "fetched_at"}
                    },
                }
                result["quality_status"] = (
                    "stale"
                    if result.get("error") and result["error"]["code"] != "not_collected"
                    else base["quality_status"]
                )
                result["fetched_at"] = base.get("fetched_at")
                if (result.get("error") or {}).get("code") == "not_collected":
                    result["error"] = None
        if result.get("observation_count"):
            result = self._analysis_freshness(result, instrument, start, today)
        result.setdefault("instrument_id", instrument_id)
        result.setdefault("source_urls", [])
        return result

    def _analysis_freshness(self, result, instrument, start, today):
        sessions = complete_sessions(
            instrument["calendar"], instrument["timezone"], start, today, self.now
        )
        expected = sessions[-1].isoformat() if sessions else None
        result["expected_latest_session"] = expected
        if expected and result.get("end_date") != expected:
            result["quality_status"] = "stale"
        return result

    def cnn_sentiment(self, *, refresh=True):
        def collect():
            payload = self.cnn.fetch(now=self.now)
            self.repository.save_sentiment(payload)
            return payload

        cnn = self._cached(
            "sentiment:US:cnn", collect, self.settings.sentiment_cache_minutes, refresh=refresh
        )
        event_time = utc_datetime(cnn.get("event_time"))
        # A successful fetch is not evidence that the observation itself is current.
        if event_time and self.now - event_time > timedelta(hours=36):
            cnn["quality_status"] = "stale"
        cnn.setdefault("provider", "cnn")
        cnn.setdefault("market", "US")
        cnn.setdefault("source_url", "https://www.cnn.com/markets/fear-and-greed")
        return cnn

    def sentiment(self, *, refresh=True):
        with ThreadPoolExecutor(max_workers=2) as pool:
            cnn = pool.submit(self.cnn_sentiment, refresh=refresh)
            vix = pool.submit(self.analysis, "INDEX_VIX", refresh=refresh)
            return {"cnn": cnn.result(), "vix": vix.result()}

    def news(self, market, instrument_id=None, window="24h", *, refresh=True, use_model=True):
        if market not in {"KR", "US"} or window not in {"24h", "7d"}:
            raise AppError(
                422, "invalid_news_filter", "뉴스 시장은 KR/US, 기간은 24h/7d여야 합니다."
            )
        instrument = get_instrument(instrument_id) if instrument_id else None
        if instrument and instrument["market"] != market:
            raise AppError(422, "market_mismatch", "종목과 뉴스 시장이 일치해야 합니다.")
        since = self.now - timedelta(hours=24) if window == "24h" else self.now - timedelta(days=7)
        provider = self.naver if market == "KR" else self.finnhub

        def collect():
            payload = provider.fetch(
                market=market, instrument=instrument, since=since, now=self.now
            )
            items = deduplicate_articles(payload["items"])
            # Leave room for UTF-8 titles, abstracts and summaries in the 1 MiB cache doc.
            truncated = payload.get("truncated", False) or len(items) > 50
            items = items[:50]
            self.repository.save_news(items)
            return {
                "items": items,
                "summary": summarize_news(items, self.settings, use_model=use_model),
                "truncated": truncated,
                "coverage_note": payload.get(
                    "coverage_note", "공급원이 반환한 제목·요약만 사용합니다."
                ),
                "event_time": max((item["published_at"] for item in items), default=None),
            }

        result = self._cached(
            f"news:{market}:{instrument_id or 'market'}:{window}",
            collect,
            self.settings.news_cache_minutes,
            refresh=refresh,
        )
        # Cached articles age out of a rolling window, even on an upstream failure.
        items = [
            item
            for item in result.get("items", [])
            if since <= utc_datetime(item["published_at"]) <= self.now + timedelta(minutes=5)
        ]
        if len(items) != len(result.get("items", [])):
            result["summary"] = summarize_news(items, self.settings, use_model=False)
        result["event_time"] = max((item["published_at"] for item in items), default=None)
        result.update(
            market=market,
            instrument_id=instrument_id,
            window=window,
            provider="naver" if market == "KR" else "finnhub",
            items=items,
        )
        result.setdefault(
            "summary",
            {
                "method": "extractive",
                "text": "뉴스를 수집한 뒤 요약을 확인할 수 있습니다.",
                "highlights": [],
            },
        )
        result.setdefault(
            "coverage_note",
            "공급원이 제공한 제목·요약 기준입니다. 전체 시장 뉴스의 완전한 목록은 아닙니다.",
        )
        result.setdefault("truncated", False)
        return result

    def overview(self, instrument_id, window="24h", *, refresh=True, use_model=True):
        instrument = get_instrument(instrument_id)
        # Independent upstream calls must not serialize their timeouts.
        with ThreadPoolExecutor(max_workers=4) as pool:
            analysis = pool.submit(self.analysis, instrument_id, refresh=refresh)
            cnn = pool.submit(self.cnn_sentiment, refresh=refresh)
            vix = (
                analysis
                if instrument_id == "INDEX_VIX"
                else pool.submit(self.analysis, "INDEX_VIX", refresh=refresh)
            )
            news = pool.submit(
                self.news,
                instrument["market"],
                instrument_id,
                window,
                refresh=refresh,
                use_model=use_model,
            )
            return {
                "instrument": instrument,
                "generated_at": self.now,
                "analysis": analysis.result(),
                "sentiment": {"cnn": cnn.result(), "vix": vix.result()},
                "news": news.result(),
            }

    def chat_context(self, instrument_id):
        """Read persisted research only. The model may explicitly collect via tools."""
        overview = self.overview(instrument_id, refresh=False)
        context = {}
        sources = []
        for name, component in [
            ("analysis", overview["analysis"]),
            ("cnn", overview["sentiment"]["cnn"]),
            ("vix", overview["sentiment"]["vix"]),
        ]:
            context[name] = {
                key: value
                for key, value in component.items()
                if key not in {"history", "calculation_notes", "source_urls"}
            }
            urls = component.get("source_urls", []) + (
                [component["source_url"]]
                if component.get("source_url") and component.get("value") is not None
                else []
            )
            sources.extend(
                {"title": name, "url": url, "provider": component.get("provider", "yahoo")}
                for url in urls
            )
        news = overview["news"]
        context["news"] = {
            key: news.get(key)
            for key in (
                "quality_status",
                "error",
                "fetched_at",
                "event_time",
                "coverage_note",
                "window",
            )
        }
        context["news"]["items"] = [
            {
                key: item[key][:350]
                if isinstance(item[key], str) and key in {"title", "summary"}
                else item[key]
                for key in ("title", "summary", "source_url", "published_at")
            }
            for item in news["items"][:5]
        ]
        sources.extend(
            {"title": item["title"], "url": item["source_url"], "provider": news["provider"]}
            for item in news["items"][:5]
        )
        return context, sources
