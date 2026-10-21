"""Bounded factual digest; no invented observations or unbounded model calls."""

from datetime import datetime, timedelta

from app.catalog import get_instrument
from app.errors import AppError
from app.services.research import utc_datetime


def build_content(research, selection, *, since=None):
    now = research.now
    market = selection["market"]
    since = since or now - timedelta(hours=24)
    floor = max(since, now - timedelta(days=7))
    warnings, sources, observations = [], [], []

    def stamp(value):
        parsed = utc_datetime(value)
        return parsed.strftime("%Y-%m-%d %H:%M UTC") if parsed else "시점 정보 없음"

    def number(value, suffix=""):
        return f"{value:,.2f}{suffix}" if isinstance(value, (int, float)) else "자료 없음"

    lines = [f"{'한국' if market == 'KR' else '미국'} 시장 브리핑", f"생성: {stamp(now)}"]
    lines.append(f"뉴스 범위: {stamp(floor)} ~ {stamp(now)} (공급원 반환 범위)")
    if floor > since:
        warnings.append("마지막 성공 전송 이후 7일을 넘는 뉴스는 수집 범위에서 제외했습니다.")
    lines.append("가격은 Yahoo 완료 일봉 기준이며 실시간 체결가가 아닙니다.")

    def source(label, url):
        if (
            isinstance(url, str)
            and url.startswith(("https://", "http://"))
            and url not in {s["url"] for s in sources}
        ):
            sources.append({"label": label[:120], "url": url})

    def quality(label, payload):
        state = payload.get("quality_status", "unavailable")
        if state != "ok" or payload.get("error"):
            warning = f"{label}: {state}"
            if payload.get("error"):
                warning += " · " + payload["error"]["message"][:200]
            warnings.append(warning)
        stamp = payload.get("data_as_of") or payload.get("event_time")
        if stamp:
            observations.append(stamp.isoformat() if isinstance(stamp, datetime) else str(stamp))
        return state

    def collect(action):
        try:
            return action()
        except AppError as exc:
            return {
                "quality_status": "unavailable",
                "error": {"code": exc.code, "message": exc.message},
            }

    def analysis(instrument_id):
        instrument = get_instrument(instrument_id)
        result = collect(lambda: research.analysis(instrument_id))
        state = quality(instrument["name"], result)
        for url in result.get("source_urls", []):
            source(instrument["name"], url)
        value = result.get("latest_close")
        if value is None:
            lines.append(f"{instrument['name']}: 관측값 없음 [{state}]")
            return

        if instrument_id == "INDEX_VIX":
            lines.append(
                f"VIX(미국 기대 변동성): {number(value)} pt / "
                f"1일 변화 {number(result.get('change_pct'), '%')} / "
                f"최근 {result.get('percentile_observations', 0)}개 종가 백분위 {number(result.get('percentile_rank'), '%')} / "
                f"기준 {stamp(result.get('data_as_of'))} [{state}]"
            )
            return
        lines.append(
            f"{instrument['name']}: {number(value)} {instrument['currency']} / "
            f"1일 {number(result.get('returns_pct', {}).get('1'), '%')} / "
            f"20일 연율 변동성 {number(result.get('realized_volatility_20d_pct'), '%')} / "
            f"RSI14 {number(result.get('rsi_14'))} / 기준 {stamp(result.get('data_as_of'))} [{state}]"
        )

    def news(instrument_id=None):
        label = get_instrument(instrument_id)["name"] if instrument_id else "시장"
        result = collect(
            lambda: research.news(
                market,
                instrument_id,
                "7d" if since < now - timedelta(hours=24) else "24h",
                use_model=False,
            )
        )
        state = quality(label + " 뉴스", result)
        if result.get("truncated"):
            warnings.append(label + " 뉴스: 공급원 결과 일부만 포함했습니다.")
        items = [
            item
            for item in result.get("items", [])
            if floor <= utc_datetime(item["published_at"]) <= now
        ]
        lines.append(f"{label} 주요 뉴스 [{state}]")
        if not items:
            lines.append(
                "해당 범위에서 확인한 기사가 없습니다."
                if state == "ok"
                else "기사를 확인할 수 없습니다."
            )
        for item in items[:3]:
            source(item["title"], item["source_url"])
            lines.append(
                f"• {item['title'][:200]} ({stamp(item['published_at'])})\n{item.get('summary', '')[:240]}\n{item['source_url']}"
            )

    indices = ["INDEX_KOSPI", "INDEX_KOSDAQ"] if market == "KR" else ["INDEX_SP500", "INDEX_NASDAQ"]
    for instrument_id in dict.fromkeys(indices + selection["instrument_ids"]):
        if instrument_id != "INDEX_VIX":
            analysis(instrument_id)
    if market == "US":
        analysis("INDEX_VIX")
        cnn = collect(research.cnn_sentiment)
        state = quality("CNN 공포탐욕지수(미국 주식)", cnn)
        source("CNN 공포탐욕지수", cnn.get("source_url"))
        lines.append(
            f"미국 주식 CNN 공포탐욕지수: {number(cnn.get('value'))} / {cnn.get('rating') or '평가 없음'} / 기준 {stamp(cnn.get('event_time'))} [{state}]"
        )
    news()
    for instrument_id in selection["instrument_ids"]:
        news(instrument_id)
    if warnings:
        lines.extend(["자료 상태·주의 사항", *dict.fromkeys(warnings)])
    lines.append("다음 확인: 최신 완료 일봉의 반영 여부, 주요 뉴스 후속 발표와 변동성 변화.")
    # Store only bounded presentation data, never full source payload/history.
    for item in sources:
        if item["url"] not in "\n".join(lines):
            lines.append(f"출처 {item['label']}: {item['url']}")
    return {
        "text": "\n".join(lines),
        "sources": sources,
        "warnings": list(dict.fromkeys(warnings)),
        "data_as_of": sorted(set(observations)),
        "generated_at": now,
    }
