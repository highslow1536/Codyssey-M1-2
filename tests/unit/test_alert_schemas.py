"""Alert schema validation tests (no network, no DB)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.alert import AlertCreate, AlertPreview, AlertUpdate, AlertValues

UTC = timezone.utc
AWARE = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
NAIVE = datetime(2026, 11, 1, 0, 0)

KRW = {"instrument_id": "KRX_005930", "currency": "KRW"}
USD = {"instrument_id": "NASDAQ_AAPL", "currency": "USD"}
POINT = {"instrument_id": "INDEX_SP500", "currency": "POINT"}


def _values(scope=None, **overrides):
    base = {
        "instrument_id": "KRX_005930",
        "currency": "KRW",
        "steps": [{"op": "cross_above", "price": "35000"}],
    }
    base.update(scope or {})
    base.update(overrides)
    return base


def test_canonical_precision_per_currency():
    assert AlertValues(**_values()).steps[0].price == "35000"
    usd = AlertValues(**_values(USD, steps=[{"op": "gte", "price": "10.5"}]))
    assert usd.steps[0].price == "10.50"
    point = AlertValues(**_values(POINT, steps=[{"op": "lt", "price": "7"}]))
    assert point.steps[0].price == "7.00"
    padded = AlertValues(**_values(steps=[{"op": "gt", "price": "35000.00"}]))
    assert padded.steps[0].price == "35000"


@pytest.mark.parametrize(
    "scope,price",
    [
        (KRW, "35000.5"),
        (USD, "10.555"),
        (POINT, "1.234"),
        (KRW, "0"),
        (USD, "-3.00"),
        (USD, "abc"),
        (USD, "NaN"),
        (USD, "Infinity"),
    ],
)
def test_bad_prices_rejected(scope, price):
    with pytest.raises(ValidationError):
        AlertValues(**_values(scope, steps=[{"op": "gt", "price": price}]))


def test_catalog_currency_must_match():
    with pytest.raises(ValidationError):
        AlertValues(**_values({"instrument_id": "KRX_005930", "currency": "USD"}))
    with pytest.raises(ValidationError):
        AlertValues(**_values({"instrument_id": "NOPE", "currency": "KRW"}))
    ok = AlertValues(**_values(USD, steps=[{"op": "gt", "price": "1.00"}]))
    assert ok.currency == "USD"


def test_all_group_valid_and_bounded():
    rule = AlertValues(
        **_values(steps=[{"all": [{"op": "gt", "price": "100"}, {"op": "lt", "price": "200"}]}])
    )
    assert rule.steps[0].all[0].price == "100"
    with pytest.raises(ValidationError):
        AlertValues(**_values(steps=[]))
    with pytest.raises(ValidationError):
        AlertValues(**_values(steps=[{"op": "gt", "price": "1"}] * 4))
    with pytest.raises(ValidationError):
        AlertValues(**_values(steps=[{"all": [{"op": "gt", "price": "1"}] * 4}]))
    with pytest.raises(ValidationError):
        AlertValues(**_values(steps=[{"op": "nope", "price": "1"}]))


def test_defaults_expiry_parsing_and_bounds():
    rule = AlertValues(**_values())
    assert rule.repeat_mode == "once"
    assert rule.session_scope == "regular"
    assert rule.active is True
    assert rule.expires_at is None
    # HTTP JSON ISO strings are parsed BEFORE the awareness check.
    via_iso = AlertValues(**_values(expires_at="2026-11-01T00:00:00+00:00"))
    assert via_iso.expires_at == AWARE
    assert AlertValues(**_values(expires_at=AWARE)).expires_at == AWARE
    with pytest.raises(ValidationError):
        AlertValues(**_values(expires_at=NAIVE))
    with pytest.raises(ValidationError):
        AlertValues(**_values(expires_at="2026-11-01T00:00:00"))
    for bad_timeout in (0, 721):
        with pytest.raises(ValidationError):
            AlertValues(**_values(step_timeout_hours=bad_timeout))
    with pytest.raises(ValidationError):
        AlertValues(**_values(session_scope="extended"))
    with pytest.raises(ValidationError):
        AlertValues(**_values(owner_uid="someone"))


def test_create_requires_idempotency_key():
    with pytest.raises(ValidationError):
        AlertCreate(**_values())
    with pytest.raises(ValidationError):
        AlertCreate(**_values(idempotency_key=""))
    with pytest.raises(ValidationError):
        AlertCreate(**_values(idempotency_key="k" * 129))
    assert AlertCreate(**_values(idempotency_key="alert-1")).idempotency_key == "alert-1"


def test_preview_has_no_idempotency_key():
    assert AlertPreview(**_values()).steps[0].price == "35000"
    with pytest.raises(ValidationError):
        AlertPreview(**_values(idempotency_key="alert-1"))


def test_update_is_full_rule_plus_revision():
    payload = _values(active=False)
    with pytest.raises(ValidationError):
        AlertUpdate(**payload)  # expected_revision is required
    update = AlertUpdate(**payload, expected_revision=3)
    assert update.reset_state is False
    assert update.active is False
    assert update.steps[0].price == "35000"
    reset = AlertUpdate(**payload, expected_revision=1, reset_state=True)
    assert reset.reset_state is True
    # Full rule still validated: bad currency match fails on update too.
    bad = _values({"instrument_id": "KRX_005930", "currency": "USD"})
    with pytest.raises(ValidationError):
        AlertUpdate(**bad, expected_revision=1)
