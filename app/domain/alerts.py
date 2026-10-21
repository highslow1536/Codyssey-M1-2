"""Pure price-alert state machine (no I/O, no wallclock, no DB).

- :func:`initial_state(now)` builds state from an aware ``now`` (creation
  or reset time). ISO strings are accepted as input but the state ALWAYS
  stores aware UTC datetimes (never ISO strings): ``stage`` (0), ``cycle``
  (0), ``last_event_time``/``last_bar_start``/``started_at``/``entered_at``/
  ``armed_at``/``last_timeout_at`` (datetime or None), ``last_session_date``
  (``YYYY-MM-DD`` str or None), ``last_price_decimal`` (str or None),
  ``evidence`` (completed-step entries of the current cycle),
  ``completed`` (bool), ``gap_pending`` (bool; set by main on resume:
  the next accepted quote uses gap semantics, then the flag clears).
- :func:`evaluate(rule, state, quote)` is pure: ``(new_state, trigger |
  None)``, inputs never mutated. ``rule`` holds AlertValues fields
  (``steps`` as ``{"op", "price"}`` / ``{"all": [...]}`` dicts,
  ``repeat_mode``, ``expires_at``, ``step_timeout_hours``, ``active``).
  ``quote`` holds ``instrument_id``/``currency``/``price_decimal`` (str),
  ``event_time``/``bar_start`` (aware datetime; ISO accepted),
  ``session_date`` (``YYYY-MM-DD``), ``gap`` (optional bool).
- All comparisons use :class:`Decimal` exactly; no float rounding.
- Ignored (state unchanged, ``None``, NO baseline update): terminal
  ``completed``; instrument/currency mismatch; ``active`` False; quote past
  ``expires_at`` (fail-safe at event time; main also filters by wallclock);
  duplicates/older (``event_time`` <= ``last_event_time``) or at/before
  ``started_at``; stage past the last step.
- Malformed rule/quote (bad op, non-positive price, naive time, empty
  steps) raises ``ValueError`` fail-closed.
- Level ops (``gt``/``gte``/``lt``/``lte``) compare the current observation
  and MAY trigger on the first observation. Crossing ops need a baseline:
  ``cross_above`` is ``prev <= threshold < new``,
  ``cross_below`` is ``prev >= threshold > new``; never on first obs.
- No crossing across sessions or gaps (same-session interval > 305s, ``gap`` True, or the ``gap_pending``
  resume flag). After a discontinuity the observation only re-baselines
  for crossing; level conditions still evaluate and a hit carries
  ``"gap": True``.
- One stage per observation: only ``steps[stage]`` is evaluated; an ``all``
  group needs EVERY member on the same observation (crossing members still
  need baseline + continuity).
- Step timeout (``event_time - armed_at`` exceeded): back to stage 0 with
  cleared cycle evidence, current observation as baseline; the timing-out
  observation never triggers.
- Final-stage hit: trigger holds ``instrument_id``/``cycle``/``stage``/
  ``event_time`` (datetime)/``price_decimal``/``evidence``/``gap``.
  ``once`` sets ``completed=True`` (stage past last index); ``repeat``
  increments ``cycle``, resets stage/evidence, re-baselines on current.
  Non-final hit advances one stage and moves ``armed_at``; a miss only
  advances the baseline.
- Evidence entries hold ``stage``/``conditions`` ([``{"op", "price"}``]),
  ``entered_at`` (datetime, when the stage was entered),
  ``event_time`` (datetime)/``price_decimal``/``gap``. ``armed_at`` is
  the current stage entry time and refreshes on every stage change.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

BAR_SECONDS = 300
GAP_AFTER_SECONDS = BAR_SECONDS + 5

LEVEL_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
CROSS_OPERATORS = frozenset({"cross_above", "cross_below"})
ALL_OPERATORS = LEVEL_OPERATORS | CROSS_OPERATORS

_STATE_KEYS = (
    "stage",
    "cycle",
    "last_event_time",
    "last_price_decimal",
    "last_session_date",
    "last_bar_start",
    "started_at",
    "entered_at",
    "armed_at",
    "evidence",
    "completed",
    "last_timeout_at",
    "gap_pending",
)


def _parse_time(value, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an aware datetime or ISO string.") from exc
    else:
        raise ValueError(f"{field} must be an aware datetime or ISO string.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return parsed


def _parse_optional_time(value, field: str):
    if value is None:
        return None
    return _parse_time(value, field)


def _parse_price(value, field: str) -> Decimal:
    try:
        price = Decimal(str(value).strip()) if isinstance(value, str) else Decimal(str(value))
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a decimal string.") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError(f"{field} must be a positive finite decimal.")
    return price


def _plain(number: Decimal) -> str:
    return format(number, "f")


def initial_state(now: datetime) -> dict:
    """Starting engine state for creation/reset time ``now`` (aware)."""
    moment = _parse_time(now, "now")
    return {
        "stage": 0,
        "cycle": 0,
        "last_event_time": None,
        "last_price_decimal": None,
        "last_session_date": None,
        "last_bar_start": None,
        "started_at": moment,
        "entered_at": moment,
        "armed_at": moment,
        "evidence": [],
        "completed": False,
        "last_timeout_at": None,
        "gap_pending": False,
    }


def _validated_rule(rule: dict) -> dict:
    if not isinstance(rule, dict):
        raise ValueError("rule must be a dict.")
    steps = rule.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 3:
        raise ValueError("rule steps must be a list of 1..3 stages.")
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"steps[{index}] must be a dict.")
        if "all" in step:
            members = step["all"]
            if not isinstance(members, list) or not 1 <= len(members) <= 3:
                raise ValueError(f"steps[{index}].all must hold 1..3 conditions.")
            if set(step.keys()) != {"all"}:
                raise ValueError(f"steps[{index}] mixes 'all' with other keys.")
            for pos, member in enumerate(members):
                _validated_simple(member, f"steps[{index}].all[{pos}]")
        else:
            _validated_simple(step, f"steps[{index}]")
    repeat_mode = rule.get("repeat_mode", "once")
    if repeat_mode not in ("once", "repeat"):
        raise ValueError("repeat_mode must be 'once' or 'repeat'.")
    timeout = rule.get("step_timeout_hours")
    if timeout is not None and (
        not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 720
    ):
        raise ValueError("step_timeout_hours must be an int in 1..720 or null.")
    expires_at = _parse_optional_time(rule.get("expires_at"), "rule expires_at")
    return {
        "steps": steps,
        "repeat_mode": repeat_mode,
        "step_timeout_hours": timeout,
        "expires_at": expires_at,
        "active": rule.get("active", True),
        "instrument_id": rule.get("instrument_id"),
        "currency": rule.get("currency"),
    }


def _validated_simple(cond: object, where: str) -> tuple[str, Decimal]:
    if not isinstance(cond, dict):
        raise ValueError(f"{where} must be a dict.")
    if set(cond.keys()) != {"op", "price"}:
        raise ValueError(f"{where} must hold exactly 'op' and 'price'.")
    op = cond["op"]
    if op not in ALL_OPERATORS:
        raise ValueError(f"{where}.op is not supported: {op!r}.")
    return op, _parse_price(cond["price"], where + ".price")


def _level_hit(op: str, current: Decimal, threshold: Decimal) -> bool:
    if op == "gt":
        return current > threshold
    if op == "gte":
        return current >= threshold
    if op == "lt":
        return current < threshold
    return current <= threshold  # lte


def _cross_hit(op: str, previous: Decimal, current: Decimal, threshold: Decimal) -> bool:
    if op == "cross_above":
        return previous <= threshold < current
    return previous >= threshold > current  # cross_below


def _copy_state(state: dict) -> dict:
    if not isinstance(state, dict):
        raise ValueError("state must be a dict.")
    copied = {key: state.get(key) for key in _STATE_KEYS}
    copied["evidence"] = [dict(item) for item in (copied["evidence"] or [])]
    for key in ("evidence",):
        for item in copied[key]:
            item["conditions"] = [dict(cond) for cond in item.get("conditions", [])]
    return copied


def evaluate(rule: dict, state: dict, quote: dict) -> tuple[dict, dict | None]:
    """Advance ``state`` with one ``quote``; return ``(new_state, trigger)``."""
    parsed_rule = _validated_rule(rule)
    new_state = _copy_state(state)
    if not isinstance(quote, dict):
        raise ValueError("quote must be a dict.")
    if new_state.get("completed") is True:
        return new_state, None

    quote_instrument = quote.get("instrument_id")
    if (
        parsed_rule["instrument_id"] is not None
        and quote_instrument is not None
        and quote_instrument != parsed_rule["instrument_id"]
    ):
        return new_state, None
    quote_currency = quote.get("currency")
    if (
        parsed_rule["currency"] is not None
        and quote_currency is not None
        and quote_currency != parsed_rule["currency"]
    ):
        return new_state, None

    current = _parse_price(quote.get("price_decimal"), "quote price_decimal")
    event_time = _parse_time(quote.get("event_time"), "quote event_time")
    bar_start = _parse_optional_time(quote.get("bar_start"), "quote bar_start")
    session_date = quote.get("session_date")
    if session_date is not None and not isinstance(session_date, str):
        raise ValueError("quote session_date must be a YYYY-MM-DD string.")
    if parsed_rule["active"] is False:
        return new_state, None
    if parsed_rule["expires_at"] is not None and event_time > parsed_rule["expires_at"]:
        return new_state, None

    started_at = _parse_optional_time(
        new_state.get("started_at", new_state.get("entered_at")), "state started_at"
    )
    if started_at is not None and event_time <= started_at:
        return new_state, None
    last_event = _parse_optional_time(new_state.get("last_event_time"), "state last_event")
    if last_event is not None and event_time <= last_event:
        return new_state, None

    # The quote is accepted: consume a pending resume discontinuity so
    # exactly one observation carries gap semantics (cleared even on a
    # miss or a step-timeout re-baseline below).
    gap_pending = new_state.get("gap_pending") is True
    new_state["gap_pending"] = False

    price_str = _plain(current)
    previous = None
    if new_state.get("last_price_decimal") is not None:
        previous = _parse_price(new_state["last_price_decimal"], "state last_price")
    last_bar = _parse_optional_time(new_state.get("last_bar_start"), "state last_bar")
    last_session = new_state.get("last_session_date")

    timeout = parsed_rule["step_timeout_hours"]
    armed_at = _parse_optional_time(new_state.get("armed_at"), "state armed_at")
    if timeout is not None and armed_at is not None:
        if (event_time - armed_at).total_seconds() > timeout * 3600:
            new_state.update(
                stage=0,
                evidence=[],
                last_event_time=event_time,
                last_price_decimal=price_str,
                armed_at=event_time,
                last_timeout_at=event_time,
            )
            if session_date is not None:
                new_state["last_session_date"] = session_date
            if bar_start is not None:
                new_state["last_bar_start"] = bar_start
            return new_state, None

    session_boundary = bool(
        previous is not None
        and session_date is not None
        and last_session is not None
        and session_date != last_session
    )
    gap = quote.get("gap") is True or session_boundary or gap_pending
    if not gap and previous is not None and last_event is not None:
        if bar_start is not None and last_bar is not None:
            delta = (bar_start - last_bar).total_seconds()
        else:
            delta = (event_time - last_event).total_seconds()
        if delta > GAP_AFTER_SECONDS:
            gap = True

    steps = parsed_rule["steps"]
    try:
        stage_index = int(new_state.get("stage", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("state stage must be an int.") from exc
    if stage_index < 0 or stage_index >= len(steps):
        return new_state, None
    try:
        cycle = int(new_state.get("cycle", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("state cycle must be an int.") from exc

    step = steps[stage_index]
    members = step["all"] if isinstance(step, dict) and "all" in step else [step]
    matched: list[dict] | None = []
    for member in members:
        op, threshold = _validated_simple(member, "rule step")
        if op in LEVEL_OPERATORS:
            if not _level_hit(op, current, threshold):
                matched = None
                break
            matched.append({"op": op, "price": _plain(threshold)})
        else:
            if previous is None or gap:
                matched = None
                break
            if not _cross_hit(op, previous, current, threshold):
                matched = None
                break
            matched.append({"op": op, "price": _plain(threshold)})

    baseline = {"last_event_time": event_time, "last_price_decimal": price_str}
    if session_date is not None:
        baseline["last_session_date"] = session_date
    if bar_start is not None:
        baseline["last_bar_start"] = bar_start

    if matched is None:
        new_state.update(baseline)
        return new_state, None

    stage_entered = armed_at if armed_at is not None else started_at
    entry = {
        "stage": stage_index,
        "conditions": matched,
        "entered_at": stage_entered,
        "event_time": event_time,
        "price_decimal": price_str,
        "gap": gap,
    }
    evidence = [*new_state.get("evidence", []), entry]
    if stage_index < len(steps) - 1:
        new_state.update(baseline)
        new_state.update(stage=stage_index + 1, armed_at=event_time, evidence=evidence)
        return new_state, None

    trigger = {
        "instrument_id": quote_instrument,
        "cycle": cycle,
        "stage": stage_index,
        "event_time": event_time,
        "price_decimal": price_str,
        "evidence": evidence,
        "gap": gap,
    }
    new_state.update(baseline)
    if parsed_rule["repeat_mode"] == "repeat":
        new_state.update(stage=0, cycle=cycle + 1, armed_at=event_time, evidence=[])
    else:
        new_state.update(stage=len(steps), evidence=evidence, completed=True)
    return new_state, trigger
