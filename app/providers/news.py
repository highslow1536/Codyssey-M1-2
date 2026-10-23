"""Naver / Finnhub news HTTP adapters (no fake data)."""

from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.errors import AppError

__all__ = [
    "normalize_url",
    "normalize_text",
    "deduplicate_articles",
    "NaverNewsProvider",
    "FinnhubNewsProvider",
]

NAVER_API_HUB_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
NAVER_LEGACY_URL = "https://openapi.naver.com/v1/search/news.json"
FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/news"
FINNHUB_COMPANY_URL = "https://finnhub.io/api/v1/company-news"
REQUEST_TIMEOUT_S = 10.0
NAVER_DISPLAY = 100
NAVER_MAX_START = 1000
NAVER_MAX_PAGES = 5
FUTURE_TOLERANCE_S = 5 * 60

NAVER_QUERIES = {
    "INDEX_KOSPI": "코스피",
    "INDEX_KOSDAQ": "코스닥",
    "KRX_005930": "삼성전자",
    "KRX_000660": "SK하이닉스",
}
NAVER_MARKET_QUERY = "증시"
FINNHUB_COMPANY_SYMBOLS = {"NASDAQ_AAPL": "AAPL", "NASDAQ_MSFT": "MSFT"}
FINNHUB_INDEX_IDS = {"INDEX_SP500", "INDEX_NASDAQ", "INDEX_VIX"}

_SCRIPT_RE = re.compile(r"<script\b.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style\b.*?</style\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def normalize_url(url: object) -> str | None:
    """Return canonical safe http(s) URL or None."""
    if not isinstance(url, str):
        return None
    s = url.strip()
    if not s or len(s.encode("utf-8")) > 2048:
        return None
    try:
        parts = urlsplit(s)
    except Exception:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    try:
        if parts.username or parts.password:
            return None
        host = parts.hostname
    except Exception:
        return None
    if not host:
        return None
    host = host.lower()
    try:
        port = parts.port
    except Exception:
        return None
    default_port = 443 if scheme == "https" else 80
    if port in (None, default_port):
        netloc = host
    else:
        netloc = host + ":" + str(port)
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except Exception:
        return None
    kept = []
    for k, v in pairs:
        lk = k.lower()
        if lk == "fbclid" or lk == "gclid" or lk.startswith("utm_"):
            continue
        kept.append((k, v))
    query = urlencode(kept, doseq=True)
    canonical = urlunsplit((scheme, netloc, parts.path, query, ""))
    return canonical if len(canonical.encode("utf-8")) <= 2048 else None


def normalize_text(value: object) -> str:
    """Strip script/style content and tags, unescape entities, collapse space."""
    if not isinstance(value, str):
        return ""
    s = _SCRIPT_RE.sub("", value)
    s = _STYLE_RE.sub("", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _article_id(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def _domain_of(canonical_url: str) -> str:
    try:
        host = urlsplit(canonical_url).hostname or ""
    except Exception:
        return ""
    return host.lower()


def deduplicate_articles(items: list[dict]) -> list[dict]:
    """Sort newest-first and drop conservative duplicates."""

    def _ts(item: dict) -> float:
        dt = item.get("published_at")
        if isinstance(dt, datetime):
            try:
                v = dt.timestamp()
            except Exception:
                return float("-inf")
            if math.isfinite(v):
                return v
        return float("-inf")

    ordered = sorted(items, key=lambda it: (-_ts(it), str(it.get("id", ""))))
    seen_urls: set[str] = set()
    seen_title_source: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for item in ordered:
        raw_url = str(item.get("source_url", ""))
        norm = normalize_url(raw_url) or raw_url.strip()
        title_key = normalize_text(item.get("title", "")).casefold()
        source_key = str(item.get("source", "")).strip().casefold()
        # Recurring daily headlines may be distinct stories on different dates.
        published = item.get("published_at")
        day = (
            published.astimezone(timezone.utc).date().isoformat()
            if isinstance(published, datetime)
            else ""
        )
        title_source_day = (title_key, source_key, day)
        if norm in seen_urls:
            continue
        if title_key and title_source_day in seen_title_source:
            continue
        seen_urls.add(norm)
        if title_key:
            seen_title_source.add(title_source_day)
        out.append(item)
    return out


def _secret_value(value: object) -> str:
    if value is None:
        return ""
    get = getattr(value, "get_secret_value", None)
    if callable(get):
        try:
            return str(get() or "").strip()
        except Exception:
            return ""
    return str(value).strip()


def _ensure_window(since: datetime, now: datetime) -> tuple[datetime, datetime]:
    if not isinstance(since, datetime) or not isinstance(now, datetime):
        raise AppError(422, "news_invalid_window", "since and now must be datetimes.")
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since_utc = since.astimezone(timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    if since_utc > now_utc:
        raise AppError(422, "news_invalid_window", "since must not be after now.")
    return since_utc, now_utc


def _validate_instrument(instrument: dict | None, market: str) -> list[str]:
    if instrument is None:
        return []
    if not isinstance(instrument, dict) or not instrument.get("id"):
        raise AppError(422, "invalid_instrument", "instrument must be a catalog dict.")
    inst_market = instrument.get("market")
    if isinstance(inst_market, str) and inst_market != market:
        raise AppError(422, "news_market_not_supported", "Market mismatch for news.")
    return [str(instrument["id"])]


def _http_get(client: httpx.Client | None, url: str, headers: dict, params: dict) -> httpx.Response:
    if client is not None:
        return client.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_S)
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as owned:
        return owned.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_S)


def _raise_for_status(provider: str, resp: httpx.Response) -> None:
    code = resp.status_code
    if code == 200:
        return
    if code in (401, 403):
        raise AppError(502, "news_provider_unauthorized", provider + " news auth failed.")
    if code == 429:
        raise AppError(503, "news_rate_limited", provider + " news rate limit exceeded.")
    if 500 <= code <= 599:
        raise AppError(503, "news_provider_unavailable", provider + " news is unavailable.")
    raise AppError(503, "news_provider_unavailable", provider + " news is unavailable.")


def _parse_json_body(provider: str, resp: httpx.Response):
    try:
        return resp.json()
    except Exception as exc:
        raise AppError(502, "news_payload_invalid", provider + " news returned bad data.") from exc


class NaverNewsProvider:
    """Naver news search adapter (KR only)."""

    def __init__(self, settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    def _credentials(self) -> tuple[str, str]:
        cid = _secret_value(getattr(self._settings, "naver_client_id", None))
        sec = _secret_value(getattr(self._settings, "naver_client_secret", None))
        if not cid or not sec:
            raise AppError(
                503,
                "news_not_configured",
                "Naver news is not configured. Set NAVER_CLIENT_ID and NAVER_CLIENT_SECRET.",
            )
        return cid, sec

    def fetch(
        self, *, market: str, instrument: dict | None, since: datetime, now: datetime
    ) -> dict:
        if market != "KR":
            raise AppError(422, "news_market_not_supported", "Naver news supports KR only.")
        since_utc, now_utc = _ensure_window(since, now)
        instrument_ids = _validate_instrument(instrument, market)
        inst_id = instrument_ids[0] if instrument_ids else None
        if inst_id:
            query = NAVER_QUERIES.get(str(inst_id), NAVER_MARKET_QUERY)
        else:
            query = NAVER_MARKET_QUERY
        cid, sec = self._credentials()
        mode = getattr(self._settings, "naver_api_mode", "api_hub")
        if mode == "api_hub":
            url = NAVER_API_HUB_URL
            headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": sec}
        elif mode == "legacy":
            url = NAVER_LEGACY_URL
            headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec}
        else:
            raise AppError(503, "news_not_configured", "Invalid NAVER_API_MODE configuration.")
        collected: list[dict] = []
        total: int | None = None
        raw_count = 0
        valid_count = 0
        covered_old = False
        pages_used = 0
        now_plus = now_utc.timestamp() + FUTURE_TOLERANCE_S
        since_ts = since_utc.timestamp()
        try:
            for page in range(NAVER_MAX_PAGES):
                start = 1 + page * NAVER_DISPLAY
                if start > NAVER_MAX_START:
                    break
                params = {"query": query, "display": NAVER_DISPLAY, "start": start, "sort": "date"}
                if mode == "api_hub":
                    params["format"] = "json"
                try:
                    resp = _http_get(self._client, url, headers, params)
                except AppError:
                    raise
                except Exception as exc:
                    raise AppError(
                        503, "news_provider_unavailable", "Naver news is unavailable."
                    ) from exc
                _raise_for_status("Naver", resp)
                payload = _parse_json_body("Naver", resp)
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise AppError(502, "news_payload_invalid", "Naver news returned bad data.")
                if total is None:
                    t = payload.get("total")
                    if isinstance(t, int) and t >= 0:
                        total = t
                    elif isinstance(t, str) and t.strip().isdigit():
                        total = int(t.strip())
                rows = payload.get("items", [])
                pages_used += 1
                if not rows:
                    break
                raw_count += len(rows)
                for raw in rows:
                    art = self._normalize_row(raw, market, instrument_ids, now_utc)
                    if art is None:
                        continue
                    valid_count += 1
                    ts = art["published_at"].timestamp()
                    if ts < since_ts:
                        covered_old = True
                        continue
                    if ts > now_plus:
                        continue
                    collected.append(art)
                page_min: float | None = None
                for raw in rows:
                    dt = self._row_time(raw)
                    if dt is not None:
                        try:
                            ts = dt.timestamp()
                        except Exception:
                            continue
                        page_min = ts if page_min is None else min(page_min, ts)
                if page_min is not None and page_min < since_ts:
                    break
                if total is not None and raw_count >= total:
                    break
                if len(rows) < NAVER_DISPLAY:
                    break
        except AppError:
            raise
        except Exception as exc:
            raise AppError(503, "news_provider_unavailable", "Naver news is unavailable.") from exc
        if raw_count and not valid_count:
            raise AppError(502, "news_payload_invalid", "Naver news returned no valid articles.")
        items = deduplicate_articles(collected)
        truncated = False
        if total is not None and total > raw_count and not covered_old:
            truncated = True
        elif total is None and pages_used >= NAVER_MAX_PAGES and not covered_old:
            truncated = True
        note = "Naver query '" + query + "' returned " + str(len(items))
        note += " articles (raw " + str(raw_count) + ", total "
        note += str(total) if total is not None else "unknown"
        note += ")."
        if truncated:
            note += " Coverage truncated at 5 pages / start<=1000."
        return {"items": items, "truncated": truncated, "query": query, "coverage_note": note}

    @staticmethod
    def _row_time(raw: object) -> datetime | None:
        if not isinstance(raw, dict):
            return None
        pub = raw.get("pubDate")
        if not isinstance(pub, str) or not pub.strip():
            return None
        try:
            dt = parsedate_to_datetime(pub.strip())
        except Exception:
            return None
        if dt is None or dt.tzinfo is None:
            return None
        try:
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _normalize_row(
        self, raw: object, market: str, instrument_ids: list[str], now_utc: datetime
    ) -> dict | None:
        if not isinstance(raw, dict):
            return None
        title = normalize_text(raw.get("title", ""))[:500]
        if not title:
            return None
        summary = normalize_text(raw.get("description", ""))[:1500]
        origin_raw = raw.get("originallink", "") or ""
        if not isinstance(origin_raw, str):
            return None
        origin_raw = origin_raw.strip()
        if origin_raw:
            canon = normalize_url(origin_raw)
            if canon is None:
                return None
        else:
            canon = normalize_url(raw.get("link", "") or "")
            if canon is None:
                return None
        dt = self._row_time(raw)
        if dt is None:
            return None
        try:
            ts = dt.timestamp()
        except Exception:
            return None
        if not math.isfinite(ts):
            return None
        return {
            "id": _article_id(canon),
            "provider": "naver",
            "provider_id": canon,
            "market": market,
            "instrument_ids": list(instrument_ids),
            "title": title,
            "summary": summary,
            "source": _domain_of(canon),
            "source_url": canon,
            "published_at": dt,
            "fetched_at": now_utc,
            "content_basis": "provider_title_and_summary",
        }


class FinnhubNewsProvider:
    """Finnhub market/company news adapter (US only)."""

    def __init__(self, settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    def _token(self) -> str:
        tok = _secret_value(getattr(self._settings, "finnhub_api_key", None))
        if not tok:
            raise AppError(
                503,
                "news_not_configured",
                "Finnhub news is not configured. Set FINNHUB_API_KEY.",
            )
        return tok

    def fetch(
        self, *, market: str, instrument: dict | None, since: datetime, now: datetime
    ) -> dict:
        if market != "US":
            raise AppError(422, "news_market_not_supported", "Finnhub news supports US only.")
        since_utc, now_utc = _ensure_window(since, now)
        instrument_ids = _validate_instrument(instrument, market)
        inst_id = instrument_ids[0] if instrument_ids else None
        symbol = FINNHUB_COMPANY_SYMBOLS.get(str(inst_id)) if inst_id else None
        token = self._token()
        headers = {"X-Finnhub-Token": token}
        if symbol is None:
            url = FINNHUB_NEWS_URL
            params = {"category": "general"}
            query = "general"
        else:
            url = FINNHUB_COMPANY_URL
            params = {
                "symbol": symbol,
                "from": since_utc.date().isoformat(),
                "to": now_utc.date().isoformat(),
            }
            query = symbol
        try:
            try:
                resp = _http_get(self._client, url, headers, params)
            except AppError:
                raise
            except Exception as exc:
                raise AppError(
                    503, "news_provider_unavailable", "Finnhub news is unavailable."
                ) from exc
            _raise_for_status("Finnhub", resp)
            payload = _parse_json_body("Finnhub", resp)
            if not isinstance(payload, list):
                raise AppError(502, "news_payload_invalid", "Finnhub news returned bad data.")
            collected: list[dict] = []
            valid_count = 0
            now_plus = now_utc.timestamp() + FUTURE_TOLERANCE_S
            since_ts = since_utc.timestamp()
            for raw in payload:
                art = self._normalize_row(raw, market, instrument_ids, now_utc)
                if art is None:
                    continue
                valid_count += 1
                ts = art["published_at"].timestamp()
                if ts < since_ts or ts > now_plus:
                    continue
                collected.append(art)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                503, "news_provider_unavailable", "Finnhub news is unavailable."
            ) from exc
        if payload and not valid_count:
            raise AppError(502, "news_payload_invalid", "Finnhub news returned no valid articles.")
        items = deduplicate_articles(collected)
        if symbol is None:
            note = "Finnhub category 'general' returned " + str(len(items)) + " articles."
        else:
            note = "Finnhub symbol '" + symbol + "' from " + str(params["from"])
            note += " to " + str(params["to"]) + " returned " + str(len(items)) + " articles."
        note += " Requested time window filters returned articles; complete historical coverage is not guaranteed."
        return {"items": items, "truncated": False, "query": query, "coverage_note": note}

    @staticmethod
    def _normalize_row(
        raw: object, market: str, instrument_ids: list[str], now_utc: datetime
    ) -> dict | None:
        if not isinstance(raw, dict):
            return None
        title = normalize_text(raw.get("headline", ""))[:500]
        if not title:
            return None
        summary = normalize_text(raw.get("summary", ""))[:1500]
        canon = normalize_url(raw.get("url", "") or "")
        if canon is None:
            return None
        dt_raw = raw.get("datetime", None)
        if isinstance(dt_raw, bool) or not isinstance(dt_raw, (int, float)):
            return None
        if not math.isfinite(float(dt_raw)):
            return None
        try:
            dt = datetime.fromtimestamp(float(dt_raw), tz=timezone.utc)
        except Exception:
            return None
        raw_id = raw.get("id", "")
        provider_id = str(raw_id)[:512] if raw_id != "" else canon
        src = raw.get("source", "")
        if isinstance(src, str) and src.strip():
            source = src.strip()[:200]
        else:
            source = _domain_of(canon)
        return {
            "id": _article_id(canon),
            "provider": "finnhub",
            "provider_id": provider_id,
            "market": market,
            "instrument_ids": list(instrument_ids),
            "title": title,
            "summary": summary,
            "source": source,
            "source_url": canon,
            "published_at": dt,
            "fetched_at": now_utc,
            "content_basis": "provider_title_and_summary",
        }
