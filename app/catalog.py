"""Instrument catalog for the bounded Yahoo slice."""

from __future__ import annotations

from app.errors import AppError

# Canonical instruments for the initial slice:
# 4 market indices + VIX + 2 KR stocks + 2 US stocks (design sections 1-2).
# currency "POINT" denotes an index level, not a tradable currency.
INSTRUMENTS: dict[str, dict] = {
    "INDEX_KOSPI": {
        "id": "INDEX_KOSPI",
        "name": "KOSPI",
        "yahoo_symbol": "^KS11",
        "market": "KR",
        "currency": "POINT",
        "timezone": "Asia/Seoul",
        "calendar": "XKRX",
        "delay_seconds": 1200,
    },
    "INDEX_KOSDAQ": {
        "id": "INDEX_KOSDAQ",
        "name": "KOSDAQ",
        "yahoo_symbol": "^KQ11",
        "market": "KR",
        "currency": "POINT",
        "timezone": "Asia/Seoul",
        "calendar": "XKRX",
        "delay_seconds": 1200,
    },
    "INDEX_SP500": {
        "id": "INDEX_SP500",
        "name": "S&P 500",
        "yahoo_symbol": "^GSPC",
        "market": "US",
        "currency": "POINT",
        "timezone": "America/New_York",
        "calendar": "XNYS",
        "delay_seconds": 0,
    },
    "INDEX_NASDAQ": {
        "id": "INDEX_NASDAQ",
        "name": "NASDAQ Composite",
        "yahoo_symbol": "^IXIC",
        "market": "US",
        "currency": "POINT",
        "timezone": "America/New_York",
        "calendar": "XNAS",
        "delay_seconds": 0,
    },
    "INDEX_VIX": {
        "id": "INDEX_VIX",
        "name": "VIX",
        "yahoo_symbol": "^VIX",
        "market": "US",
        "currency": "POINT",
        "timezone": "America/New_York",
        "calendar": "XCBF",
        "delay_seconds": 0,
    },
    "KRX_005930": {
        "id": "KRX_005930",
        "name": "Samsung Electronics",
        "yahoo_symbol": "005930.KS",
        "market": "KR",
        "currency": "KRW",
        "timezone": "Asia/Seoul",
        "calendar": "XKRX",
        "delay_seconds": 1200,
    },
    "KRX_000660": {
        "id": "KRX_000660",
        "name": "SK Hynix",
        "yahoo_symbol": "000660.KS",
        "market": "KR",
        "currency": "KRW",
        "timezone": "Asia/Seoul",
        "calendar": "XKRX",
        "delay_seconds": 1200,
    },
    "NASDAQ_AAPL": {
        "id": "NASDAQ_AAPL",
        "name": "Apple",
        "yahoo_symbol": "AAPL",
        "market": "US",
        "currency": "USD",
        "timezone": "America/New_York",
        "calendar": "XNAS",
        "delay_seconds": 0,
    },
    "NASDAQ_MSFT": {
        "id": "NASDAQ_MSFT",
        "name": "Microsoft",
        "yahoo_symbol": "MSFT",
        "market": "US",
        "currency": "USD",
        "timezone": "America/New_York",
        "calendar": "XNAS",
        "delay_seconds": 0,
    },
}


def get_instrument(instrument_id: str) -> dict:
    """Return a copy of the catalog entry or raise 404 AppError."""
    try:
        entry = INSTRUMENTS[instrument_id]
    except KeyError:
        raise AppError(
            404, "instrument_not_found", "Unknown instrument: " + str(instrument_id)
        ) from None
    return dict(entry)


def list_instruments(market: str | None = None) -> list[dict]:
    """Return catalog entries, optionally filtered by market ('KR'/'US')."""
    items = [dict(v) for v in INSTRUMENTS.values()]
    if market is None:
        return items
    return [item for item in items if item.get("market") == market]
