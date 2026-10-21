"""Alert state-machine tests: pure transitions, no network, no DB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.alerts import evaluate, initial_state

UTC = timezone.utc
T0 = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)

RULE_CROSS_THEN_LOW = {
    "instrument_id": "KRX_005930",
    "currency": "KRW",
    "steps": [{"op": "cross_above", "price": "35000"}, {"op": "lte", "price": "30000"}],
    "repeat_mode": "once",
    "active": True,
}


def quote(price, moment, session="2026-10-20", **extra):
    payload = {
        "instrument_id": "KRX_005930",
        "currency": "KRW",
        "price_decimal": str(price),
        "event_time": moment,
        "bar_start": moment - timedelta(minutes=5),
        "session_date": session,
    }
    payload.update(extra)
    return payload


def test_initial_state_shape():
    state = initial_state(T0)
    assert state["stage"] == 0
    assert state["cycle"] == 0
    assert state["completed"] is False
    assert state["last_event_time"] is None
    assert state["last_price_decimal"] is None
    assert state["evidence"] == []
    assert state["started_at"] == T0
    assert state["armed_at"] == T0
    assert state["entered_at"] == T0
    assert state["gap_pending"] is False


def test_level_may_trigger_on_first_observation():
    rule = dict(RULE_CROSS_THEN_LOW, steps=[{"op": "gt", "price": "35000"}])
    state, trigger = evaluate(rule, initial_state(T0), quote(36000, T0 + timedelta(minutes=5)))
    assert trigger is not None
    assert trigger["cycle"] == 0
    assert trigger["price_decimal"] == "36000"
    assert trigger["gap"] is False
    assert state["completed"] is True


def test_crossing_needs_baseline_first_observation_only_baselines():
    state, trigger = evaluate(
        RULE_CROSS_THEN_LOW, initial_state(T0), quote(36000, T0 + timedelta(minutes=5))
    )
    assert trigger is None
    assert state["stage"] == 0
    assert state["last_price_decimal"] == "36000"


def test_cross_above_from_equal_baseline_advances_stage():
    state = initial_state(T0)
    state, _ = evaluate(RULE_CROSS_THEN_LOW, state, quote(35000, T0 + timedelta(minutes=5)))
    # prev == threshold satisfies prev <= threshold < new.
    state, trigger = evaluate(RULE_CROSS_THEN_LOW, state, quote(35001, T0 + timedelta(minutes=10)))
    assert trigger is None  # non-final stage: advance only
    assert state["stage"] == 1
    assert state["evidence"][0]["conditions"] == [{"op": "cross_above", "price": "35000"}]


def test_cross_above_no_retrigger_when_already_above():
    rule = dict(RULE_CROSS_THEN_LOW, steps=[{"op": "cross_above", "price": "35000"}])
    state = initial_state(T0)
    state, _ = evaluate(rule, state, quote(34000, T0 + timedelta(minutes=5)))
    state, first = evaluate(rule, state, quote(36000, T0 + timedelta(minutes=10)))
    assert first is not None
    assert state["completed"] is True


def test_cross_below_direction():
    rule = dict(RULE_CROSS_THEN_LOW, steps=[{"op": "cross_below", "price": "30000"}])
    state = initial_state(T0)
    state, _ = evaluate(rule, state, quote(31000, T0 + timedelta(minutes=5)))
    state, trigger = evaluate(rule, state, quote(29999, T0 + timedelta(minutes=10)))
    assert trigger is not None
    state2 = initial_state(T0)
    state2, _ = evaluate(rule, state2, quote(29000, T0 + timedelta(minutes=5)))
    state2, miss = evaluate(rule, state2, quote(28000, T0 + timedelta(minutes=10)))
    assert miss is None  # prev 29000 >= 30000 is False


def test_one_stage_per_observation():
    rule = dict(
        RULE_CROSS_THEN_LOW,
        steps=[{"op": "gt", "price": "100"}, {"op": "gt", "price": "200"}],
    )
    state, trigger = evaluate(rule, initial_state(T0), quote(300, T0 + timedelta(minutes=5)))
    assert trigger is None
    assert state["stage"] == 1
    state, trigger = evaluate(rule, state, quote(300, T0 + timedelta(minutes=10)))
    assert trigger is not None


def test_sequential_flow_completes_once_with_evidence():
    state = initial_state(T0)
    state, _ = evaluate(RULE_CROSS_THEN_LOW, state, quote(34000, T0 + timedelta(minutes=5)))
    state, _ = evaluate(RULE_CROSS_THEN_LOW, state, quote(36000, T0 + timedelta(minutes=10)))
    assert state["stage"] == 1
    state, trigger = evaluate(RULE_CROSS_THEN_LOW, state, quote(29000, T0 + timedelta(minutes=15)))
    assert trigger is not None
    assert trigger["cycle"] == 0
    assert [e["stage"] for e in trigger["evidence"]] == [0, 1]
    assert state["completed"] is True
    frozen, again = evaluate(RULE_CROSS_THEN_LOW, state, quote(10000, T0 + timedelta(minutes=20)))
    assert again is None
    assert frozen["completed"] is True


def test_repeat_increments_cycle_and_rebaselines():
    rule = dict(
        RULE_CROSS_THEN_LOW,
        steps=[{"op": "cross_above", "price": "35000"}],
        repeat_mode="repeat",
    )
    state = initial_state(T0)
    state, _ = evaluate(rule, state, quote(34000, T0 + timedelta(minutes=5)))
    state, trigger = evaluate(rule, state, quote(36000, T0 + timedelta(minutes=10)))
    assert trigger is not None
    assert trigger["cycle"] == 0
    assert state["cycle"] == 1
    assert state["stage"] == 0
    assert state["completed"] is False
    assert state["evidence"] == []
    # Same observation price reused: current is the new baseline, so a flat
    # next quote must not cross again.
    state, again = evaluate(rule, state, quote(36000, T0 + timedelta(minutes=15)))
    assert again is None


def test_duplicates_older_and_started_at_ignored():
    rule = dict(RULE_CROSS_THEN_LOW, steps=[{"op": "gt", "price": "1"}])
    moment = T0 + timedelta(minutes=5)
    state, _ = evaluate(rule, initial_state(T0), quote(10, moment))
    # completed once-rule ignores everything afterwards
    same, trigger = evaluate(rule, state, quote(999, moment))
    assert trigger is None
    # fresh repeat rule: duplicate and older quotes are ignored
    repeating = dict(rule, repeat_mode="repeat", steps=[{"op": "cross_above", "price": "35000"}])
    fresh = initial_state(T0)
    fresh, _ = evaluate(repeating, fresh, quote(34000, moment))
    dup, trigger = evaluate(repeating, fresh, quote(99999, moment))
    assert trigger is None
    assert dup["last_price_decimal"] == "34000"
    older, trigger = evaluate(repeating, fresh, quote(99999, T0 + timedelta(minutes=1)))
    assert trigger is None
    at_start, trigger = evaluate(repeating, fresh, quote(99999, T0))
    assert trigger is None


def test_gap_suppresses_crossing_but_level_reports_gap():
    cross = dict(RULE_CROSS_THEN_LOW, steps=[{"op": "cross_above", "price": "35000"}])
    state = initial_state(T0)
    state, _ = evaluate(cross, state, quote(34000, T0 + timedelta(minutes=5)))
    gapped, trigger = evaluate(cross, state, quote(36000, T0 + timedelta(minutes=30), gap=True))
    assert trigger is None
    assert gapped["last_price_decimal"] == "36000"
    repeating = dict(
        RULE_CROSS_THEN_LOW, steps=[{"op": "gt", "price": "35000"}], repeat_mode="repeat"
    )
    state3 = initial_state(T0)
    state3, _ = evaluate(repeating, state3, quote(100, T0 + timedelta(minutes=5)))
    state3, trigger = evaluate(
        repeating, state3, quote(99999, T0 + timedelta(minutes=40), gap=True)
    )
    assert trigger is not None
    assert trigger["gap"] is True
    assert trigger["evidence"][0]["gap"] is True


def test_session_change_suppresses_crossing():
    cross = dict(RULE_CROSS_THEN_LOW, steps=[{"op": "cross_above", "price": "35000"}])
    state = initial_state(T0)
    state, _ = evaluate(cross, state, quote(34000, T0 + timedelta(minutes=5)))
    moved, trigger = evaluate(
        cross, state, quote(36000, T0 + timedelta(minutes=10), session="2026-10-21")
    )
    assert trigger is None
    assert moved["last_session_date"] == "2026-10-21"


def test_step_timeout_resets_without_trigger():
    rule = dict(RULE_CROSS_THEN_LOW, step_timeout_hours=1)
    state = initial_state(T0)
    state, _ = evaluate(rule, state, quote(34000, T0 + timedelta(minutes=5)))
    late = T0 + timedelta(hours=2)
    reset, trigger = evaluate(rule, state, quote(99999, late))
    assert trigger is None
    assert reset["stage"] == 0
    assert reset["last_price_decimal"] == "99999"
    assert reset["last_timeout_at"] == late


def test_expiry_inactive_and_mismatch_ignored():
    expiring = dict(
        RULE_CROSS_THEN_LOW,
        steps=[{"op": "gt", "price": "1"}],
        expires_at=(T0 + timedelta(minutes=6)).isoformat(),
    )
    state, trigger = evaluate(expiring, initial_state(T0), quote(10, T0 + timedelta(minutes=10)))
    assert trigger is None
    assert state["last_price_decimal"] is None
    paused = dict(RULE_CROSS_THEN_LOW, steps=[{"op": "gt", "price": "1"}], active=False)
    state, trigger = evaluate(paused, initial_state(T0), quote(10, T0 + timedelta(minutes=5)))
    assert trigger is None
    wrong = quote(
        10,
        T0 + timedelta(minutes=5),
    )
    wrong["instrument_id"] = "NASDAQ_AAPL"
    state, trigger = evaluate(RULE_CROSS_THEN_LOW, initial_state(T0), wrong)
    assert trigger is None
    fx = quote(10, T0 + timedelta(minutes=5))
    fx["currency"] = "USD"
    state, trigger = evaluate(RULE_CROSS_THEN_LOW, initial_state(T0), fx)
    assert trigger is None


def test_all_group_matches_on_same_observation():
    rule = dict(
        RULE_CROSS_THEN_LOW,
        steps=[{"all": [{"op": "gt", "price": "100"}, {"op": "lt", "price": "200"}]}],
    )
    state, trigger = evaluate(rule, initial_state(T0), quote(150, T0 + timedelta(minutes=5)))
    assert trigger is not None
    state, trigger = evaluate(rule, initial_state(T0), quote(250, T0 + timedelta(minutes=5)))
    assert trigger is None


def test_iso_inputs_accepted_but_outputs_are_datetimes():
    rule = dict(
        RULE_CROSS_THEN_LOW,
        steps=[{"op": "gt", "price": "1"}],
        expires_at=(T0 + timedelta(hours=1)).isoformat(),
    )
    state = initial_state(T0.isoformat())
    assert state["started_at"] == T0
    moment = T0 + timedelta(minutes=5)
    payload = quote(10, moment)
    payload["event_time"] = moment.isoformat()
    payload["bar_start"] = (moment - timedelta(minutes=5)).isoformat()
    state, trigger = evaluate(rule, state, payload)
    assert trigger is not None
    assert trigger["event_time"] == moment
    assert state["last_event_time"] == moment
    assert state["last_bar_start"] == moment - timedelta(minutes=5)
    assert trigger["evidence"][0]["event_time"] == moment
    assert set(trigger["evidence"][0]) >= {"event_time", "price_decimal"}


def test_malformed_inputs_raise():
    state = initial_state(T0)
    good_quote = quote(10, T0 + timedelta(minutes=5))
    with pytest.raises(ValueError):
        evaluate({"steps": []}, state, good_quote)
    with pytest.raises(ValueError):
        evaluate({"steps": [{"op": "nope", "price": "10"}]}, state, good_quote)
    with pytest.raises(ValueError):
        evaluate(RULE_CROSS_THEN_LOW, state, quote(10, T0.replace(tzinfo=None)))
    with pytest.raises(ValueError):
        initial_state(T0.replace(tzinfo=None))


def test_inputs_not_mutated():
    rule = dict(RULE_CROSS_THEN_LOW)
    snapshot_rule = {"steps": [dict(s) for s in rule["steps"]]}
    state = initial_state(T0)
    before = dict(state)
    evaluate(rule, state, quote(34000, T0 + timedelta(minutes=5)))
    assert state == before
    assert rule["steps"] == snapshot_rule["steps"]


def test_gap_pending_labels_first_level_hit_then_clears():
    rule = dict(RULE_CROSS_THEN_LOW, steps=[{"op": "gt", "price": "35000"}], repeat_mode="repeat")
    state = initial_state(T0)
    state["gap_pending"] = True  # main sets this on resume with armed progress
    first, trigger = evaluate(rule, state, quote(36000, T0 + timedelta(minutes=5)))
    assert trigger is not None
    assert trigger["gap"] is True
    assert first["gap_pending"] is False
    assert first["cycle"] == 1
    # Flag consumed: the next hit is a plain non-gap trigger.
    _, second = evaluate(rule, first, quote(36001, T0 + timedelta(minutes=10)))
    assert second is not None
    assert second["gap"] is False


def test_gap_pending_suppresses_crossing_then_clears():
    rule = dict(RULE_CROSS_THEN_LOW, steps=[{"op": "cross_above", "price": "35000"}])
    state = initial_state(T0)
    state, _ = evaluate(rule, state, quote(34000, T0 + timedelta(minutes=5)))
    state["gap_pending"] = True
    moved, trigger = evaluate(rule, state, quote(36000, T0 + timedelta(minutes=10)))
    assert trigger is None
    assert moved["gap_pending"] is False
    assert moved["last_price_decimal"] == "36000"


def test_evidence_entered_at_tracks_stage_entry():
    state = initial_state(T0)
    first_hit = T0 + timedelta(minutes=10)
    state, _ = evaluate(RULE_CROSS_THEN_LOW, state, quote(34000, T0 + timedelta(minutes=5)))
    state, _ = evaluate(RULE_CROSS_THEN_LOW, state, quote(36000, first_hit))
    assert state["evidence"][0]["entered_at"] == T0
    assert state["armed_at"] == first_hit
    state, trigger = evaluate(RULE_CROSS_THEN_LOW, state, quote(29000, T0 + timedelta(minutes=15)))
    assert trigger is not None
    assert trigger["evidence"][0]["entered_at"] == T0
    assert trigger["evidence"][1]["entered_at"] == first_hit
