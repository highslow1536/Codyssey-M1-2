from collections import Counter
from statistics import fmean


def summarize(records: list[dict], instrument: dict) -> dict:
    rows = sorted(records, key=lambda row: (row["date"], row["id"]))
    values = [row["value"] for row in rows]
    change = (values[-1] / values[-21] - 1) * 100 if len(values) >= 21 else None
    trend = "insufficient_data"
    if change is not None:
        trend = "rising" if change > 1 else "falling" if change < -1 else "flat"
    dates = [row["date"] for row in rows]
    return {
        "instrument_id": instrument["id"],
        "instrument_name": instrument["name"],
        "currency": instrument["currency"],
        "dataset": "user_analysis_data",
        "count": len(rows),
        "unique_dates": len(set(dates)),
        "duplicate_date_count": len(dates) - len(set(dates)),
        "start_date": min(dates) if rows else None,
        "end_date": max(dates) if rows else None,
        "average": fmean(values) if values else None,
        "maximum": max(values) if values else None,
        "minimum": min(values) if values else None,
        "latest_value": values[-1] if values else None,
        "trend": trend,
        "trend_return_pct": change,
        "trend_period_observations": 20,
        "trend_threshold_pct": 1,
        "source_counts": dict(Counter(row["source"] for row in rows)),
        "user_modified_count": sum(row.get("user_modified", False) for row in rows),
        "price_basis": sorted({row.get("price_basis", "user_value") for row in rows}),
        "source_urls": sorted({row["source_url"] for row in rows if row.get("source_url")}),
        "data_as_of": max(
            (row["event_time"] for row in rows if row.get("event_time")), default=None
        ),
        "quality_status": "ok" if rows else "unavailable",
        "missing_count": None,
        "missing_count_note": "거래일 달력과의 결측 비교는 기술 분석 단계에서 제공합니다.",
    }
