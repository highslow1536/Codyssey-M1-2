from app.domain.summary import summarize

INSTRUMENT = {"id": "TEST", "name": "Test", "currency": "USD"}


def test_empty_summary_has_no_fabricated_zero():
    result = summarize([], INSTRUMENT)
    assert result["count"] == 0
    assert result["average"] is None
    assert result["trend"] == "insufficient_data"


def test_summary_trend_requires_previous_20_observations():
    rows = [
        {
            "id": str(i),
            "date": f"2025-01-{i + 1:02}",
            "value": 100 + i,
            "source": "manual",
            "user_modified": True,
        }
        for i in range(21)
    ]
    result = summarize(rows, INSTRUMENT)
    assert result["count"] == 21
    assert result["average"] == 110
    assert result["minimum"] == 100
    assert result["maximum"] == 120
    assert abs(result["trend_return_pct"] - 20) < 1e-10
    assert result["trend"] == "rising"
    assert result["user_modified_count"] == 21
