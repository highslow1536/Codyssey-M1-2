"""Price-alert request schemas (pure validation; no DB or network).

- Amounts are canonical positive decimal strings (:func:`canonical_price`)
  compared as :class:`Decimal` by the engine. Thresholds are never rounded.
- ``currency`` is ``KRW``/``USD``/``POINT`` and MUST match the catalog entry
  for ``instrument_id`` (checked via :func:`app.catalog.get_instrument`).
  Precision: KRW 0 decimals, USD 2, POINT 2.
- ``steps``: 1..3 sequential stages; each is ``{"op", "price"}`` or an AND
  group ``{"all": [1..3 simple conditions]}`` matched on one observation.
  Ops: ``gt``/``gte``/``lt``/``lte`` (level) and
  ``cross_above``/``cross_below`` (crossing).
- ``session_scope`` is ``"regular"``; ``repeat_mode`` defaults ``"once"``;
  ``expires_at`` (aware datetime or ISO string, ``None`` = no expiry) is
  validated AFTER parsing so HTTP JSON strings are accepted; naive values
  are rejected. ``step_timeout_hours`` 1..720 or ``None``; ``active``
  defaults ``True``.
- :class:`AlertCreate` adds ``idempotency_key`` (1..128).
- :class:`AlertUpdate` extends :class:`AlertValues` with ALL rule fields
  (full PATCH: UI/backend always send the complete rule) plus required
  ``expected_revision >= 1`` and ``reset_state`` (default ``False``;
  ``True`` directs main to discard engine state via ``initial_state``).
- :class:`AlertPreview` is :class:`AlertValues` with no idempotency key.
- Pause/resume/cancel are owned by main via :class:`AlertUpdate`
  (``active`` flag) and document delete; no ``AlertAction`` model here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Union

from pydantic import Field, field_validator, model_validator

from app.catalog import get_instrument
from app.errors import AppError
from app.schemas.data import StrictModel

AlertOp = Literal["gt", "gte", "lt", "lte", "cross_above", "cross_below"]
AlertCurrency = Literal["KRW", "USD", "POINT"]
SessionScope = Literal["regular"]
RepeatMode = Literal["once", "repeat"]

CURRENCIES: tuple[str, str, str] = ("KRW", "USD", "POINT")
PRICE_DECIMALS: dict[str, int] = {"KRW": 0, "USD": 2, "POINT": 2}
OPERATORS: tuple[str, ...] = ("gt", "gte", "lt", "lte", "cross_above", "cross_below")
LEVEL_OPERATORS: frozenset[str] = frozenset({"gt", "gte", "lt", "lte"})
CROSS_OPERATORS: frozenset[str] = frozenset({"cross_above", "cross_below"})


def canonical_price(raw: str, currency: str) -> str:
    """Canonical threshold string: positive finite Decimal at fixed precision.

    Exactly representable input is normalized (USD ``"10.5"`` -> ``"10.50"``,
    KRW ``"35000.00"`` -> ``"35000"``); anything needing rounding is
    REJECTED so a threshold never moves silently. Raises ValueError.
    """
    if currency not in PRICE_DECIMALS:
        raise ValueError("unknown currency: " + str(currency))
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError("price must be a decimal string: " + str(raw)) from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("price must be a positive finite decimal.")
    places = PRICE_DECIMALS[currency]
    quantum = Decimal(1) if places == 0 else Decimal(1).scaleb(-places)
    try:
        quantized = value.quantize(quantum)
    except InvalidOperation as exc:
        raise ValueError(f"price precision for {currency} is {places} decimals.") from exc
    if quantized != value:
        raise ValueError(f"price precision for {currency} is {places} decimals.")
    return format(quantized, "f")


def _positive_decimal_string(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("price must be a decimal string.")
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("price must be a decimal string: " + raw) from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("price must be a positive finite decimal.")
    return raw.strip()


class SimpleStep(StrictModel):
    op: AlertOp
    price: str

    _price_ok = field_validator("price", mode="before")(_positive_decimal_string)


class AllStep(StrictModel):
    all: list[SimpleStep] = Field(min_length=1, max_length=3)


Step = Union[SimpleStep, AllStep]


class AlertValues(StrictModel):
    instrument_id: str = Field(min_length=1, max_length=80)
    currency: AlertCurrency
    steps: list[Step] = Field(min_length=1, max_length=3)
    session_scope: SessionScope = "regular"
    repeat_mode: RepeatMode = "once"
    expires_at: datetime | None = None
    step_timeout_hours: int | None = Field(default=None, ge=1, le=720)
    active: bool = True

    @field_validator("expires_at", mode="after")
    @classmethod
    def _expires_must_be_aware(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expires_at must be timezone-aware.")
        return value

    @model_validator(mode="after")
    def _check_catalog_and_prices(self):
        try:
            entry = get_instrument(self.instrument_id)
        except AppError as exc:
            raise ValueError("unknown instrument: " + self.instrument_id) from exc
        if entry.get("currency") != self.currency:
            raise ValueError(
                f"currency {self.currency} does not match catalog"
                f" {entry.get('currency')} for {self.instrument_id}."
            )
        for step in self.steps:
            simples = step.all if isinstance(step, AllStep) else [step]
            for simple in simples:
                simple.price = canonical_price(simple.price, self.currency)
        return self


class AlertCreate(AlertValues):
    idempotency_key: str = Field(min_length=1, max_length=128)


class AlertPreview(AlertValues):
    """Preview request: same as values, no idempotency key (extra=forbid)."""


class AlertUpdate(AlertValues):
    """Full-rule PATCH: complete rule fields plus revision guards.

    UI/backend always send the whole rule; ``expected_revision`` guards
    concurrent edits and ``reset_state=True`` directs main to discard
    engine state via ``initial_state`` instead of carrying over.
    """

    expected_revision: int = Field(ge=1)
    reset_state: bool = False
