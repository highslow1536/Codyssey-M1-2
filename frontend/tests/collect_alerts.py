"""Collect genuine Yahoo quotes into disposable emulator history for browser QA.

Never imported by runtime code. No Discord sends. Run with APP_ENV=test,
FIREBASE_PROJECT_ID=demo-stock-assistant, FIRESTORE_EMULATOR_HOST=127.0.0.1:8080,
and PYTHONPATH=. from the repository root before alerts-browser.mjs.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from app.catalog import get_instrument
from app.providers.quotes import YahooQuoteProvider
from app.repositories.alerts import AlertRepository
from app.schemas.alert import AlertCreate
from app.services.alerts import event_content


def main():
    if (
        os.getenv("APP_ENV") != "test"
        or os.getenv("FIREBASE_PROJECT_ID") != "demo-stock-assistant"
        or os.getenv("FIRESTORE_EMULATOR_HOST") != "127.0.0.1:8080"
    ):
        raise RuntimeError("Requires the local disposable demo-stock-assistant emulator")
    now = datetime.now(timezone.utc)
    repo = AlertRepository(
        Client(project="demo-stock-assistant", credentials=AnonymousCredentials())
    )
    results = []
    for instrument_id in ["KRX_005930", "NASDAQ_AAPL"]:
        quotes = YahooQuoteProvider().fetch(get_instrument(instrument_id), now)
        if not quotes:
            raise RuntimeError("Real Yahoo intraday observations are required")
        token = repo.claim_poll(instrument_id, now, 5)
        if token:
            if not repo.finish_poll(
                instrument_id, token, datetime.now(timezone.utc), quotes=quotes
            ):
                raise RuntimeError("Quote collection lease expired")
        # Freeze registration immediately before the genuine historical series.
        # The threshold is a test rule; every observed price remains unmodified.
        rule = AlertCreate(
            instrument_id=instrument_id,
            currency=quotes[-1]["currency"],
            steps=[{"op": "gt", "price": "1"}],
            idempotency_key="live-validation-" + instrument_id,
        ).model_dump()
        created = repo.create(
            "browser-test-owner", rule, quotes[0]["event_time"] - timedelta(minutes=1)
        )
        events = repo.apply("browser-test-owner", created["id"], quotes[:80], now, event_content)
        results.append(
            {
                "instrument_id": instrument_id,
                "actual_quote_count": len(quotes),
                "latest": quotes[-1],
                "events_created": events,
                "rule_id": created["id"],
                "cached_count": len(repo.cached_quotes(instrument_id, now)),
            }
        )
    artifact = Path("artifacts/alerts-live-storage.json")
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(json.dumps(results, default=str, ensure_ascii=False, indent=2))
    print(json.dumps(results, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
