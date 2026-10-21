"""Unit tests for pure technical indicator calculations (no network)."""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

import pytest

from app.catalog import get_instrument
from app.domain.indicators import calculate_indicators

SP = get_instrument("INDEX_SP500")
VIX = get_instrument("INDEX_VIX")


def _bar(day, close, volume=1000, instrument_id="INDEX_SP500", source="https://x"):
    return {
        "instrument_id": instrument_id,
        "date": day,
        "adjusted_close": close,
        "volume": volume,
        "event_time": datetime.fromisoformat(day + "T16:00:00+00:00"),
        "fetched_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "source_url": source,
    }


def _closes(n, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


def _days(n):
    return ["2025-01-" + str(i + 1).zfill(2) for i in range(n)]


def test_returns_closed_form():
    closes = [100.0, 102.0, 105.0, 110.0, 121.0, 130.0]
    days = ["2025-01-0" + str(i + 1) for i in range(6)]
    bars = [_bar(d, c) for d, c in zip(days, closes)]
    out = calculate_indicators(bars, SP)
    assert out["returns_pct"]["1"] == (130.0 / 121.0 - 1) * 100
    assert out["returns_pct"]["5"] == (130.0 / 100.0 - 1) * 100
    assert out["returns_pct"]["20"] is None
    assert out["returns_pct"]["60"] is None
    assert out["latest_close"] == 130.0
    assert out["provider"] == "yahoo"
    assert out["dataset"] == "market_bars"
    assert out["price_basis"] == "adjusted_close"


def test_insufficient_observations_null_with_notes():
    bars = [_bar(d, 100.0 + i) for i, d in enumerate(_days(5))]
    out = calculate_indicators(bars, SP)
    assert out["observation_count"] == 5
    assert out["quality_status"] == "insufficient_data"
    assert out["latest_close"] == 104.0
    assert out["returns_pct"]["1"] == pytest.approx((104.0 / 103.0 - 1) * 100)
    assert out["returns_pct"]["20"] is None
    assert out["realized_volatility_20d_pct"] is None
    assert out["sma_20"] is None
    assert out["sma_60"] is None
    assert out["rsi_14"] is None
    assert out["volume_ratio_20d"] is None
    assert out["trend"] == "insufficient_data"
    joined = " ".join(out["calculation_notes"])
    assert "21" in joined
    assert "61" in joined
    assert "rsi" not in out


def test_empty_bars_insufficient_quality():
    out = calculate_indicators([], SP)
    assert out["observation_count"] == 0
    assert out["quality_status"] == "insufficient_data"
    assert out["latest_close"] is None
    assert out["start_date"] is None
    assert out["source_urls"] == []


def test_volatility_matches_sample_variance_closed_form():
    closes = [100 + i for i in range(21)]
    bars = [_bar(d, c, volume=1000) for d, c in zip(_days(21), closes)]
    out = calculate_indicators(bars, SP)
    logs = [math.log(closes[i] / closes[i - 1]) for i in range(1, 21)]
    expected = statistics.stdev(logs) * math.sqrt(252) * 100
    assert out["realized_volatility_20d_pct"] == expected
    flat = [100.0] * 21
    flat_bars = [_bar(d, c) for d, c in zip(_days(21), flat)]
    flat_out = calculate_indicators(flat_bars, SP)
    assert flat_out["realized_volatility_20d_pct"] == 0.0


def test_rsi_flat_gains_only_losses_only():
    flat = [_bar(d, 100.0) for d in _days(15)]
    assert calculate_indicators(flat, SP)["rsi_14"] == 50.0
    gains = [_bar(d, 100.0 + i) for i, d in enumerate(_days(15))]
    assert calculate_indicators(gains, SP)["rsi_14"] == 100.0
    losses = [_bar(d, 100.0 - i) for i, d in enumerate(_days(15))]
    assert calculate_indicators(losses, SP)["rsi_14"] == 0.0


def test_rsi_wilder_recursion_matches_manual():
    closes = [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
        46.00,
        46.03,
        46.41,
    ]
    bars = [
        _bar(d, c)
        for d, c in zip(["2025-02-" + str(i + 1).zfill(2) for i in range(len(closes))], closes)
    ]
    out = calculate_indicators(bars, SP)
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_g = sum(d for d in diffs[:14] if d > 0) / 14
    avg_l = sum(-d for d in diffs[:14] if d < 0) / 14
    for d in diffs[14:]:
        avg_g = (avg_g * 13 + (d if d > 0 else 0.0)) / 14
        avg_l = (avg_l * 13 + (-d if d < 0 else 0.0)) / 14
    expected = 100 - (100 / (1 + avg_g / avg_l))
    assert abs(out["rsi_14"] - expected) < 1e-9


def test_filters_nan_nonpositive_duplicates_and_other_instruments():
    bars = [
        _bar("2025-01-01", 100.0, volume=1000),
        _bar("2025-01-02", float("nan"), volume=1000),
        _bar("2025-01-02", 0.0, volume=1000),
        _bar("2025-01-02", 102.0, volume=1100),
        _bar("2025-01-02", 103.0, volume=1200),
        _bar("2025-01-03", 104.0, volume=1300, instrument_id="INDEX_NASDAQ"),
        _bar("2025-01-03", 105.0, volume=1300),
        {"instrument_id": "INDEX_SP500", "date": "bad-date", "adjusted_close": 99.0},
    ]
    out = calculate_indicators(bars, SP)
    assert out["observation_count"] == 3
    assert out["start_date"] == "2025-01-01"
    assert out["end_date"] == "2025-01-03"
    assert out["latest_close"] == 105.0
    assert out["source_urls"] == ["https://x"]
    assert out["data_as_of"].tzinfo is not None
    assert out["fetched_at"].tzinfo is not None
    assert any("Dropped" in n for n in out["calculation_notes"])


def test_sma_and_distance_and_trend_thresholds():
    closes = _closes(61, start=100.0, step=1.0)
    from datetime import date as _d
    from datetime import timedelta as _td

    _base = _d(2025, 3, 1)
    _ds61 = [(_base + _td(days=i)).isoformat() for i in range(61)]
    bars = [_bar(d, c) for d, c in zip(_ds61, closes)]
    out = calculate_indicators(bars, SP)
    assert out["sma_20"] == sum(closes[-20:]) / 20
    assert out["sma_60"] == sum(closes[-60:]) / 60
    assert out["sma_20_distance_pct"] == pytest.approx(
        (closes[-1] - out["sma_20"]) / out["sma_20"] * 100
    )
    assert out["trend"] == "rising"
    assert out["trend_return_pct"] == out["returns_pct"]["20"]
    assert out["quality_status"] == "ok"
    assert out["trend_period_observations"] == 20
    assert out["trend_threshold_pct"] == 1.0
    falling = _closes(21, start=120.0, step=-1.0)
    fbars = [_bar(d, c) for d, c in zip(_days(21), falling)]
    assert calculate_indicators(fbars, SP)["trend"] == "falling"
    flatish = [_bar(d, 100.0) for d in _days(21)]
    assert calculate_indicators(flatish, SP)["trend"] == "flat"


def test_volume_ratio_requires_full_positive_window():
    vols = [1000] * 20 + [2000]
    bars = [_bar(d, 100 + i, volume=v) for i, (d, v) in enumerate(zip(_days(21), vols))]
    assert calculate_indicators(bars, SP)["volume_ratio_20d"] == 2.0
    bad_prior = vols.copy()
    bad_prior[5] = 0
    bad_bars = [_bar(d, 100 + i, volume=v) for i, (d, v) in enumerate(zip(_days(21), bad_prior))]
    bad_out = calculate_indicators(bad_bars, SP)
    assert bad_out["volume_ratio_20d"] is None
    assert any("volume_ratio_20d" in n for n in bad_out["calculation_notes"])
    missing = [_bar(d, 100 + i, volume=v) for i, (d, v) in enumerate(zip(_days(21), vols))]
    missing[3] = _bar(_days(21)[3], 100 + 3)
    del missing[3]["volume"]
    missing_out = calculate_indicators(missing, SP)
    assert missing_out["volume_ratio_20d"] is None
    zero_latest = [_bar(d, 100 + i, volume=v) for i, (d, v) in enumerate(zip(_days(21), vols))]
    zero_latest[-1]["volume"] = 0
    assert calculate_indicators(zero_latest, SP)["volume_ratio_20d"] is None


def test_vix_extra_percentile_and_change():
    closes = [float(20 + (i % 30)) for i in range(30)]
    bars = [_bar(d, c, instrument_id="INDEX_VIX") for d, c in zip(_days(30), closes)]
    out = calculate_indicators(bars, VIX)
    assert out["change_pct"] == out["returns_pct"]["1"]
    latest = closes[-1]
    expected_rank = sum(1 for c in closes[-30:] if c <= latest) / 30 * 100
    assert out["percentile_rank"] == expected_rank
    assert out["percentile_observations"] == 30
    assert "vix" not in out
    assert "vix_extra" not in out
    assert "vix_context" not in out
    plain = calculate_indicators([_bar(d, c) for d, c in zip(_days(30), closes)], SP)
    assert "vix" not in plain
    assert "change_pct" not in plain
    assert "percentile_rank" not in plain
    assert "percentile_observations" not in plain
