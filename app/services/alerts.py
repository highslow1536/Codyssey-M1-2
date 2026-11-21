"""Five-minute real observation monitoring and durable conditional Discord alerts."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from app.catalog import get_instrument
from app.config import get_settings
from app.domain.sessions import _require_calendar
from app.errors import AppError
from app.providers.discord import split_content
from app.providers.quotes import YahooQuoteProvider
from app.repositories.alerts import AlertRepository
from app.services.delivery import drain_outbox
from app.worker_logging import log_worker_issue

logger = logging.getLogger(__name__)

OPERATORS = {
    "gt": "초과",
    "gte": "이상",
    "lt": "미만",
    "lte": "이하",
    "cross_above": "상승 돌파",
    "cross_below": "하락 돌파",
}


def describe(rule):
    def condition(item):
        return f"{item['price']} {rule['currency']} {OPERATORS[item['op']]}"

    stages = [
        " 그리고 ".join(condition(c) for c in step["all"]) if "all" in step else condition(step)
        for step in rule["steps"]
    ]
    return " → 이후 관측에서 ".join(stages)


def preview(rule):
    instrument = get_instrument(rule["instrument_id"])
    return {
        "description": describe(rule),
        "rule": rule,
        "observation": {
            "interval_minutes": 5,
            "session_scope": "regular",
            "delay_seconds": instrument["delay_seconds"],
        },
        "warnings": [
            "정규장 5분 봉 종가를 비교합니다. 봉 내부의 순간 돌파는 놓칠 수 있습니다.",
            "최초 관측은 돌파의 기준값입니다. 공백을 가로질러 돌파를 추정하지 않습니다.",
            "Yahoo 시세 지연과 worker 실행 간격만큼 알림이 늦을 수 있습니다.",
        ],
    }


def monitoring_window(instrument, now, poll_minutes):
    """Fetch only during the real regular session and its delayed closing tail."""
    calendar = _require_calendar(instrument["calendar"])
    day = now.astimezone(ZoneInfo(instrument["timezone"])).date()
    try:
        label = pd.Timestamp(day)
        if not calendar.is_session(label):
            return False
        opened = calendar.session_open(label).to_pydatetime()
        closed = calendar.session_close(label).to_pydatetime()
        return (
            opened + timedelta(minutes=5)
            <= now
            <= closed
            + timedelta(seconds=instrument["delay_seconds"] + 300 + poll_minutes * 60 + 120)
        )
    except Exception as exc:
        raise AppError(
            503, "calendar_unavailable", "거래소 정규장 일정을 확인하지 못했습니다."
        ) from exc


def event_content(rule, trigger, quote, event_id, now):
    instrument = get_instrument(rule["instrument_id"])
    description = describe(rule)
    lines = [
        f"가격 조건 확인 · {instrument['name']}",
        description,
        f"관측 종가: {quote['price_decimal']} {rule['currency']}",
        f"관측 시각(UTC): {quote['event_time'].isoformat()}",
        f"확인 시각(UTC): {now.isoformat()}",
        "기준: Yahoo 정규장 5분 봉 종가",
        f"안내 지연: {instrument['delay_seconds'] // 60}분",
    ]
    for i, evidence in enumerate(trigger.get("evidence", []), 1):
        lines.append(
            f"단계 {i} 근거: {evidence.get('price_decimal', '')} / {evidence.get('event_time', '')}"
        )
    if trigger.get("gap"):
        lines.append("공백 이후 첫 관측에서 확인했습니다. 실제 최초 발생 시각은 알 수 없습니다.")
    lines.append(quote["source_url"])
    text = "\n".join(lines)
    return {
        "instrument_id": rule["instrument_id"],
        "currency": rule["currency"],
        "generated_at": now,
        "event_time": trigger["event_time"],
        "price_decimal": trigger["price_decimal"],
        "description": description,
        "text": text,
        "cycle": trigger["cycle"],
        "gap": trigger.get("gap", False),
        "evidence": trigger.get("evidence", []),
        "source_url": quote["source_url"],
        "delivery_status": "queued",
        "parts": AlertRepository._parts(split_content(text, event_id, kind="alert")),
    }


class AlertService:
    def __init__(self, db, settings=None, *, provider=None, discord=None, clock=None):
        self.settings = settings or get_settings()
        self.repository = AlertRepository(db)
        self.provider = provider or YahooQuoteProvider(self.settings)
        self.discord = discord
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def retry(self, uid, event_id, key):
        event = self.repository.get(uid, event_id)
        if not self.settings.discord_webhook_url:
            raise AppError(
                503, "discord_not_configured", "서버의 DISCORD_WEBHOOK_URL을 설정하세요."
            )
        return self.repository.queue(
            uid, event_id, key, split_content(event["text"], event_id, kind="alert"), self.clock()
        )

    def tick(self, uid, *, part_limit=30):
        now = self.clock()
        rules = [
            r
            for r in self.repository.list_rules(uid)
            if r["active"] and not r["state"]["completed"]
        ]
        counts = {"quotes_fetched": 0, "alerts_triggered": 0, "monitor_errors": 0}
        instruments = {}
        for rule in rules:
            if rule.get("expires_at") and rule["expires_at"] <= now:
                self.repository.status(
                    uid, rule["id"], "expired", now, pause=True, revision=rule["revision"]
                )
            else:
                instruments.setdefault(rule["instrument_id"], []).append(rule)
        for instrument_id, selected in instruments.items():
            instrument = get_instrument(instrument_id)
            stage = "session_calendar"
            details = {}
            try:
                if not monitoring_window(instrument, now, self.settings.data_poll_minutes):
                    for rule in selected:
                        self.repository.status(
                            uid, rule["id"], "market_closed", now, revision=rule["revision"]
                        )
                    continue
                stage = "claim_quote_poll"
                token = self.repository.claim_poll(
                    instrument_id, now, self.settings.data_poll_minutes
                )
                if token:
                    stage = "fetch_quotes"
                    try:
                        quotes = self.provider.fetch(instrument, now)
                    except Exception as exc:
                        error = AppError(
                            503, "quotes_unavailable", "Yahoo 장중 시세를 수집하지 못했습니다."
                        )
                        stage = "store_poll_failure"
                        self.repository.finish_poll(
                            instrument_id,
                            token,
                            self.clock(),
                            error={"code": error.code, "message": error.message},
                        )
                        stage = "fetch_quotes"
                        raise error from exc
                    stage = "store_quotes"
                    details["bars_fetched"] = len(quotes)
                    if not self.repository.finish_poll(
                        instrument_id, token, self.clock(), quotes=quotes
                    ):
                        log_worker_issue(
                            logger, "alert_poll_lease_lost", instrument_id=instrument_id, **details
                        )
                    counts["quotes_fetched"] += len(quotes)
                stage = "check_quote_freshness"
                latest = self.repository.latest_quote(instrument_id)
                freshness = timedelta(
                    seconds=instrument["delay_seconds"]
                    + 300
                    + self.settings.data_poll_minutes * 60
                    + 120
                )
                if not latest or now - latest["event_time"] > freshness:
                    details["latest_bar_at"] = latest["event_time"].isoformat() if latest else None
                    details["max_age_seconds"] = int(freshness.total_seconds())
                    raise AppError(
                        503, "stale_quotes", "최신 완성 봉이 오래되어 새 조건 판정을 중지했습니다."
                    )
                earliest = min(
                    r["state"].get("last_event_time")
                    or r["state"].get("started_at")
                    or r["created_at"]
                    for r in selected
                )
                stage = "load_cached_quotes"
                quotes = self.repository.cached_quotes(instrument_id, now, earliest)
                stage = "evaluate_rule"
                for rule in selected:
                    watermark = (
                        rule["state"].get("last_event_time")
                        or rule["state"].get("started_at")
                        or rule["created_at"]
                    )
                    pending = [q for q in quotes if q["event_time"] > watermark]
                    for start in range(0, len(pending), 80):
                        counts["alerts_triggered"] += self.repository.apply(
                            uid,
                            rule["id"],
                            pending[start : start + 80],
                            self.clock(),
                            event_content,
                            revision=rule["revision"],
                        )
            except Exception as exc:
                log_worker_issue(
                    logger,
                    "alert_monitor_failed",
                    exc=exc,
                    instrument_id=instrument_id,
                    stage=stage,
                    affected_rules=len(selected),
                    code=exc.code if isinstance(exc, AppError) else "unexpected_error",
                    **details,
                )
                if not isinstance(exc, AppError):
                    raise
                counts["monitor_errors"] += 1
                for rule in selected:
                    self.repository.status(
                        uid,
                        rule["id"],
                        "unavailable" if exc.code != "stale_quotes" else "stale",
                        self.clock(),
                        {"code": exc.code, "message": exc.message},
                        revision=rule["revision"],
                    )
        counts.update(
            drain_outbox(
                self.repository,
                uid,
                self.settings,
                self.clock,
                discord=self.discord,
                part_limit=part_limit,
            )
        )
        return counts
