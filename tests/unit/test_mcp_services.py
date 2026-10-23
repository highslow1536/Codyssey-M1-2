"""Adapter service reuse, model budgets and explicit environment selection."""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.config import Settings, get_settings
from app.errors import AppError
from mcp_adapter.server import load_settings, public_json, validate_settings
from mcp_adapter.tools import TOOLS, ToolService


def settings(**kwargs):
    return Settings(
        _env_file=None,
        firebase_project_id="demo-stock-assistant",
        mcp_owner_uid="owner",
        allowed_user_uids="owner",
        **kwargs,
    )


@pytest.mark.parametrize(
    "name,raw",
    [
        ("summarize_dataset", {"instrument_id": "NASDAQ_AAPL", "start_date": "2026-01-01"}),
        ("search_news", {"market": "KR", "window": "7d"}),
        ("get_us_sentiment", {}),
        ("get_market_analysis", {"instrument_id": "INDEX_SP500"}),
    ],
)
def test_existing_chat_tools_use_the_same_dispatcher(monkeypatch, name, raw):
    sentinel = {"quality_status": "stale", "source_url": "https://example.test/source"}
    shared = MagicMock(return_value=sentinel)
    monkeypatch.setattr("mcp_adapter.tools.execute_tool", shared)
    db = MagicMock()
    args = TOOLS[name][0].model_validate(raw)
    assert ToolService(db, settings()).read(name, args) is sentinel
    shared.assert_called_once_with(
        db, owner_uid="owner", name=name, arguments=args.model_dump(mode="json")
    )


def test_overview_respects_market_and_cannot_launch_nested_model(monkeypatch):
    overview = MagicMock(
        return_value={
            "news": {"items": list(range(20))},
            "sentiment": {"cnn": {"history": list(range(100)), "value": 50}},
        }
    )
    factory = MagicMock(return_value=MagicMock(overview=overview))
    monkeypatch.setattr("mcp_adapter.tools.ResearchService", factory)
    service = ToolService(MagicMock(), settings())
    args = TOOLS["get_market_overview"][0].model_validate({"market": "KR"})
    result = service.read("get_market_overview", args)
    overview.assert_called_once_with("INDEX_KOSPI", "24h", use_model=False)
    assert result["news"]["items"] == list(range(5))
    assert result["sentiment"]["cnn"] == {"value": 50}
    args.instrument_id = "NASDAQ_AAPL"
    with pytest.raises(AppError, match="시장"):
        service.read("get_market_overview", args)


def test_public_results_keep_exact_prices_and_times_without_internal_fields():
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    out = public_json(
        {"owner_uid": "owner", "_secret": "hidden", "items": [{"price": "35000", "at": now}]}
    )
    assert out == {"items": [{"price": "35000", "at": "2026-10-23T00:00:00+00:00"}]}


def test_explicit_env_file_is_used_by_all_existing_services(tmp_path, monkeypatch):
    env_file = tmp_path / "server.env"
    env_file.write_text(
        "FIREBASE_PROJECT_ID=demo-stock-assistant\n"
        "MCP_OWNER_UID=file-owner\nALLOWED_USER_UIDS=file-owner\nFINNHUB_API_KEY=test-only\n"
    )
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.setenv("STOCK_ASSISTANT_ENV_FILE", str(env_file))
    try:
        loaded = load_settings(str(env_file))
        assert loaded is get_settings()
        assert get_settings().finnhub_api_key == "test-only"
        monkeypatch.setenv("MCP_OWNER_UID", "override")
        monkeypatch.setenv("ALLOWED_USER_UIDS", "override")
        assert load_settings(str(env_file)).mcp_owner_uid == "override"
    finally:
        get_settings.cache_clear()


def test_credential_path_must_be_absolute():
    with pytest.raises(AppError) as caught:
        validate_settings(settings(google_application_credentials="relative.json"))
    assert caught.value.code == "configuration_invalid"


def test_history_schema_only_supports_daily_dates():
    from pydantic import ValidationError

    model = TOOLS["get_price_history"][0]
    assert model(instrument_id="NASDAQ_AAPL", start_date="2026-01-01").start_date == date(
        2026, 1, 1
    )
    with pytest.raises(ValidationError):
        model(instrument_id="NASDAQ_AAPL", interval="1m")
