"""Pure technical market calculations over Yahoo daily bars (no synthetic data)."""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timezone
from typing import Any

from app.errors import AppError

PROVIDER = "yahoo"
DATASET = "market_bars"
PRICE_BASIS = "adjusted_close"
ANNUALIZATION = 252
VOL_WINDOW = 20
SMA_SHORT = 20
SMA_LONG = 60
RSI_PERIOD = 14
TREND_PERIOD = 20
TREND_THRESHOLD_PCT = 1.0
VIX_ID = "INDEX_VIX"
LOOKBACK_252 = 252


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return None
        try:
            return date.fromisoformat(text_value[:10])
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text_value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _to_aware_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        stamp = float(value)
        if stamp >= 1e11:
            stamp = stamp / 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return None
        try:
            dt = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _parse_close(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num <= 0:
        return None
    return num


def _parse_volume(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num < 0:
        return None
    return num


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def calculate_indicators(bars: list[dict], instrument: dict) -> dict:
    """Compute pure technical indicators from Yahoo daily bars."""
    if not isinstance(instrument, dict):
        raise AppError(422, "invalid_instrument", "instrument must be a catalog dict.")
    instrument_id = instrument.get("id")
    if not instrument_id or not isinstance(instrument_id, str):
        raise AppError(422, "invalid_instrument", "instrument is missing: id.")
    instrument_id = str(instrument_id)
    instrument_name = str(instrument.get("name", instrument_id))
    currency = str(instrument.get("currency", ""))
    if not isinstance(bars, list):
        raise AppError(422, "invalid_bars", "bars must be a list of Yahoo daily bars.")
    kept: dict[str, dict] = {}
    skipped_other_instrument = 0
    skipped_bad = 0
    for bar in bars:
        if not isinstance(bar, dict):
            skipped_bad += 1
            continue
        bar_instrument = bar.get("instrument_id")
        if bar_instrument is not None and str(bar_instrument) != instrument_id:
            skipped_other_instrument += 1
            continue
        day = _to_date(bar.get("date"))
        if day is None:
            skipped_bad += 1
            continue
        close = _parse_close(bar.get("adjusted_close"))
        if close is None:
            skipped_bad += 1
            continue
        key = day.isoformat()
        kept[key] = {
            "date": day,
            "close": close,
            "volume": _parse_volume(bar.get("volume")),
            "event_time": _to_aware_utc(bar.get("event_time")),
            "fetched_at": _to_aware_utc(bar.get("fetched_at")),
            "source_url": bar.get("source_url"),
        }
    dates_sorted = sorted(kept.keys())
    closes = [kept[k]["close"] for k in dates_sorted]
    volumes: list[float | None] = [kept[k]["volume"] for k in dates_sorted]
    n = len(dates_sorted)
    notes: list[str] = [
        "Price basis is Yahoo adjusted_close; user-editable data values are not used.",
        "Only bars for the requested catalog instrument are used; other instruments are ignored.",
        "Bars with non-finite or non-positive adjusted_close are dropped; "
        "duplicate dates keep the last input occurrence, then rows are sorted by date.",
        "Realized volatility uses consecutive observed closes with no interpolation "
        "of missing trading days; annualized with a 252 trading-day assumption "
        "(sample standard deviation of log returns times sqrt(252) times 100).",
        "RSI-14 uses Wilder smoothing seeded on the first 14 price differences; "
        "flat history gives 50, gains-only gives 100, losses-only gives 0, "
        "then the Wilder recursion runs over all remaining bars.",
        "Volume ratio uses the latest volume over the mean of the prior 20 positive "
        "volumes; missing or zero volumes are not dropped to emulate a full window.",
    ]
    if skipped_other_instrument:
        notes.append("Ignored " + str(skipped_other_instrument) + " bar(s) from other instruments.")
    if skipped_bad:
        notes.append("Dropped " + str(skipped_bad) + " bar(s) with bad date/adjusted_close.")
    if n == 0:
        notes.append("No usable bars after filtering; all computed fields are null.")
    source_urls = sorted(
        {str(kept[k]["source_url"]) for k in dates_sorted if kept[k]["source_url"]}
    )
    event_times = [kept[k]["event_time"] for k in dates_sorted if kept[k]["event_time"]]
    fetched_list = [kept[k]["fetched_at"] for k in dates_sorted if kept[k]["fetched_at"]]
    data_as_of = max(event_times) if event_times else None
    fetched_at = max(fetched_list) if fetched_list else None
    if n > 0 and data_as_of is None:
        notes.append("data_as_of is null: no parseable bar event_time.")
    if n > 0 and fetched_at is None:
        notes.append("fetched_at is null: no parseable bar fetched_at.")
    start_date = dates_sorted[0] if n else None
    end_date = dates_sorted[-1] if n else None
    quality_status = "ok" if n >= VOL_WINDOW + 1 else "insufficient_data"
    if 0 < n < VOL_WINDOW + 1:
        notes.append(
            "Short history: 20-day indicators require 21 observations and "
            "60-day indicators require 60 (SMA) / 61 (return) observations; "
            "available shorter-window values are still returned."
        )
    latest_close: float | None = closes[-1] if n else None
    returns_pct: dict[str, float | None] = {}
    for period in (1, 5, 20, 60):
        key = str(period)
        if n >= period + 1:
            base = closes[n - 1 - period]
            if base and base > 0:
                returns_pct[key] = float((closes[-1] / base - 1.0) * 100.0)
            else:
                returns_pct[key] = None
                notes.append("returns_pct['" + key + "']: null base price.")
        else:
            returns_pct[key] = None
            notes.append(
                "returns_pct['"
                + key
                + "']: requires "
                + str(period + 1)
                + " observations; have "
                + str(n)
                + "."
            )
    realized_volatility_20d_pct: float | None = None
    if n >= VOL_WINDOW + 1:
        window = closes[n - (VOL_WINDOW + 1) :]
        log_returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
        if len(log_returns) >= 2:
            sd = statistics.stdev(log_returns)
            realized_volatility_20d_pct = float(sd * math.sqrt(ANNUALIZATION) * 100.0)
        else:
            notes.append("realized_volatility_20d_pct: not enough log returns.")
    else:
        notes.append("realized_volatility_20d_pct: requires 21 observations; have " + str(n) + ".")
    sma_20 = _mean(closes[n - SMA_SHORT :]) if n >= SMA_SHORT else None
    if sma_20 is None:
        notes.append("sma_20: requires 20 observations; have " + str(n) + ".")
    sma_60 = _mean(closes[n - SMA_LONG :]) if n >= SMA_LONG else None
    if sma_60 is None:
        notes.append("sma_60: requires 60 observations; have " + str(n) + ".")
    sma_20_distance_pct = (
        float((latest_close - sma_20) / sma_20 * 100.0)
        if latest_close is not None and sma_20
        else None
    )
    if sma_20 is not None and sma_20_distance_pct is None:
        notes.append("sma_20_distance_pct: unavailable despite sma_20.")
    elif sma_20 is None:
        notes.append("sma_20_distance_pct: null because sma_20 is null.")
    sma_60_distance_pct = (
        float((latest_close - sma_60) / sma_60 * 100.0)
        if latest_close is not None and sma_60
        else None
    )
    if sma_60 is None:
        notes.append("sma_60_distance_pct: null because sma_60 is null.")
    rsi_14: float | None = None
    if n >= RSI_PERIOD + 1:
        diffs = [closes[i] - closes[i - 1] for i in range(1, n)]
        first = diffs[:RSI_PERIOD]
        avg_gain = sum(d for d in first if d > 0) / RSI_PERIOD
        avg_loss = sum(-d for d in first if d < 0) / RSI_PERIOD
        for d in diffs[RSI_PERIOD:]:
            gain = d if d > 0 else 0.0
            loss = -d if d < 0 else 0.0
            avg_gain = (avg_gain * (RSI_PERIOD - 1) + gain) / RSI_PERIOD
            avg_loss = (avg_loss * (RSI_PERIOD - 1) + loss) / RSI_PERIOD
        if avg_gain == 0 and avg_loss == 0:
            rsi_14 = 50.0
        elif avg_loss == 0:
            rsi_14 = 100.0
        elif avg_gain == 0:
            rsi_14 = 0.0
        else:
            rs = avg_gain / avg_loss
            rsi_14 = float(100.0 - (100.0 / (1.0 + rs)))
    else:
        notes.append("rsi_14: requires 15 observations; have " + str(n) + ".")
    volume_ratio_20d: float | None = None
    if n >= VOL_WINDOW + 1:
        latest_vol = volumes[-1]
        prior = volumes[n - (VOL_WINDOW + 1) : n - 1]
        if latest_vol is None or latest_vol <= 0:
            notes.append("volume_ratio_20d: null because latest volume is missing or <= 0.")
        elif len(prior) < VOL_WINDOW or any(v is None or v <= 0 for v in prior):
            notes.append(
                "volume_ratio_20d: null because at least one of the prior 20 volumes "
                "is missing or <= 0; missing values are not dropped to emulate a window."
            )
        else:
            mean_prior = sum(prior) / VOL_WINDOW  # type: ignore[arg-type]
            volume_ratio_20d = float(latest_vol / mean_prior) if mean_prior else None
    else:
        notes.append("volume_ratio_20d: requires 21 observations; have " + str(n) + ".")
    trend_return = returns_pct.get("20")
    if trend_return is None:
        trend = "insufficient_data"
        notes.append("trend: insufficient_data because 20-day return is unavailable.")
    elif trend_return > TREND_THRESHOLD_PCT:
        trend = "rising"
    elif trend_return < -TREND_THRESHOLD_PCT:
        trend = "falling"
    else:
        trend = "flat"
    result: dict = {
        "instrument_id": instrument_id,
        "instrument_name": instrument_name,
        "currency": currency,
        "provider": PROVIDER,
        "dataset": DATASET,
        "price_basis": PRICE_BASIS,
        "observation_count": n,
        "start_date": start_date,
        "end_date": end_date,
        "data_as_of": data_as_of,
        "fetched_at": fetched_at,
        "source_urls": source_urls,
        "quality_status": quality_status,
        "latest_close": latest_close,
        "returns_pct": returns_pct,
        "realized_volatility_20d_pct": realized_volatility_20d_pct,
        "sma_20": sma_20,
        "sma_60": sma_60,
        "sma_20_distance_pct": sma_20_distance_pct,
        "sma_60_distance_pct": sma_60_distance_pct,
        "rsi_14": rsi_14,
        "volume_ratio_20d": volume_ratio_20d,
        "trend": trend,
        "trend_return_pct": trend_return,
        "trend_period_observations": TREND_PERIOD,
        "trend_threshold_pct": TREND_THRESHOLD_PCT,
        "calculation_notes": notes,
    }
    if instrument_id == VIX_ID:
        window = closes[max(0, n - LOOKBACK_252) :]
        pct_obs = len(window)
        if pct_obs and latest_close is not None:
            le_count = sum(1 for c in window if c <= latest_close)
            percentile_rank = float(le_count / pct_obs * 100.0)
        else:
            percentile_rank = None
            notes.append("VIX percentile_rank: unavailable with no observations.")
        result["change_pct"] = returns_pct.get("1")
        result["percentile_rank"] = percentile_rank
        result["percentile_observations"] = pct_obs
        notes.append(
            "VIX change_pct equals the 1-day return; percentile_rank is the empirical "
            "percentile of the latest close within the last 252 closes including "
            "the latest (<= latest / observations * 100)."
        )
    return result
