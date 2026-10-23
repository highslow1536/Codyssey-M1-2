"""Tick worker orchestration with stub services (no cloud or Discord)."""

from __future__ import annotations

import json
import signal
import sys
import threading
import time

import pytest

from jobs import tick


class StubService:
    def __init__(self, result=None, exc=None, *, on_tick=None):
        self.result = {} if result is None else result
        self.exc = exc
        self.calls: list = []
        self.on_tick = on_tick

    def tick(self, uid):
        self.calls.append(uid)
        if self.on_tick is not None:
            self.on_tick()
        if self.exc is not None:
            raise self.exc
        return self.result


def _json_lines(capsys):
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _patch_main_deps(monkeypatch, briefings, alerts, argv):
    captured: dict = {}

    class _Settings:
        job_owner_uid = "owner-1"
        allowed_uids = ["owner-1"]

    def _fake_install(event):
        captured["event"] = event
        return {}

    monkeypatch.setattr(tick, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tick, "get_firestore", lambda: object())
    monkeypatch.setattr(tick, "BriefingService", lambda db, settings: briefings)
    monkeypatch.setattr(tick, "AlertService", lambda db, settings: alerts)
    monkeypatch.setattr(tick, "_install_shutdown_handlers", _fake_install)
    monkeypatch.setattr(sys, "argv", argv)
    return captured


def test_run_once_logs_each_service_result(capsys):
    services = {
        "briefings": StubService({"generated": 1}),
        "alerts": StubService({"alerts_triggered": 2}),
    }
    assert tick.run_once(services, "owner-1") is False
    lines = _json_lines(capsys)
    assert {"briefings": {"generated": 1}} in lines
    assert {"alerts": {"alerts_triggered": 2}} in lines
    assert [s.calls for s in services.values()] == [["owner-1"], ["owner-1"]]


def test_failed_tick_continues_other_job_and_hides_details(capsys):
    services = {
        "briefings": StubService(exc=RuntimeError("upstream boom secret-text")),
        "alerts": StubService({"alerts_triggered": 1}),
    }
    assert tick.run_once(services, "owner-1") is True
    lines = _json_lines(capsys)
    assert {"error": "briefings_tick_failed"} in lines
    assert {"alerts": {"alerts_triggered": 1}} in lines
    assert services["alerts"].calls == ["owner-1"]
    raw = json.dumps(lines)
    assert "upstream boom" not in raw
    assert "secret-text" not in raw


def test_run_once_keyboard_interrupt_requests_shutdown(capsys):
    shutdown = threading.Event()
    services = {
        "briefings": StubService(exc=KeyboardInterrupt()),
        "alerts": StubService({}),
    }
    assert tick.run_once(services, "owner-1", shutdown) is True
    assert shutdown.is_set()
    assert services["alerts"].calls == []


def test_run_once_skips_remaining_work_after_shutdown(capsys):
    shutdown = threading.Event()
    shutdown.set()
    services = {
        "briefings": StubService({"generated": 1}),
        "alerts": StubService({}),
    }
    tick.run_once(services, "owner-1", shutdown)
    assert services["briefings"].calls == []
    assert services["alerts"].calls == []
    assert _json_lines(capsys) == []


def test_loop_does_no_work_when_shutdown_already_requested():
    shutdown = threading.Event()
    shutdown.set()
    services = {"briefings": StubService({}), "alerts": StubService({})}
    tick.run_loop(services, "owner-1", 60, shutdown)
    assert services["briefings"].calls == []
    assert services["alerts"].calls == []


def test_loop_finishes_current_tick_but_skips_next_service(capsys):
    shutdown = threading.Event()
    services = {
        "briefings": StubService({"generated": 1}, on_tick=shutdown.set),
        "alerts": StubService({"alerts_triggered": 1}),
    }
    tick.run_loop(services, "owner-1", 60, shutdown)
    assert services["briefings"].calls == ["owner-1"]
    assert services["alerts"].calls == []
    lines = _json_lines(capsys)
    assert {"briefings": {"generated": 1}} in lines


def test_loop_idle_wait_ends_promptly_on_shutdown():
    shutdown = threading.Event()
    timer = threading.Timer(0.05, shutdown.set)
    timer.start()
    try:
        start = time.monotonic()
        tick.run_loop({"briefings": StubService({})}, "owner-1", 3600, shutdown)
        elapsed = time.monotonic() - start
    finally:
        timer.cancel()
    assert elapsed < 5


def test_loop_continues_after_failed_tick(capsys):
    shutdown = threading.Event()
    flaky = StubService(exc=RuntimeError("boom"))
    calls = {"n": 0}

    def _count_and_stop():
        calls["n"] += 1
        if calls["n"] >= 2:
            shutdown.set()

    services = {
        "briefings": StubService({"generated": 0}, on_tick=_count_and_stop),
        "alerts": flaky,
    }
    # Short interval: multi-iteration behavior without a real 60 s idle wait.
    tick.run_loop(services, "owner-1", 0.05, shutdown)
    # The loop survived the first failed alerts tick and ran a second iteration;
    # shutdown during the second briefings tick then skipped the next service.
    assert calls["n"] == 2
    assert flaky.calls == ["owner-1"]
    lines = _json_lines(capsys)
    assert lines.count({"error": "alerts_tick_failed"}) == 1
    assert lines.count({"briefings": {"generated": 0}}) == 2


def test_install_handlers_sets_event_on_sigterm(monkeypatch):
    seen: dict = {}

    def _fake(sig, handler):
        seen[sig] = handler
        return None

    monkeypatch.setattr(signal, "signal", _fake)
    shutdown = threading.Event()
    tick._install_shutdown_handlers(shutdown)
    assert signal.SIGTERM in seen
    assert signal.SIGINT in seen
    seen[signal.SIGTERM](signal.SIGTERM, None)
    assert shutdown.is_set()


def test_parse_args_defaults_and_bounds():
    args = tick.parse_args([])
    assert args.loop is False
    assert args.interval == 60
    args = tick.parse_args(["--loop", "--interval", "10"])
    assert args.loop is True
    assert args.interval == 10
    with pytest.raises(SystemExit):
        tick.parse_args(["--interval", "9"])
    with pytest.raises(SystemExit):
        tick.parse_args(["--interval", "3601"])


def test_main_one_shot_success_logs_startup_and_shutdown(monkeypatch, capsys):
    briefings = StubService({"generated": 1})
    alerts = StubService({"alerts_triggered": 0})
    _patch_main_deps(monkeypatch, briefings, alerts, ["tick"])
    tick.main()
    lines = _json_lines(capsys)
    assert lines[0] == {"event": "tick_startup", "mode": "once", "interval": 60}
    assert {"briefings": {"generated": 1}} in lines
    assert {"alerts": {"alerts_triggered": 0}} in lines
    assert lines[-1] == {"event": "tick_shutdown", "mode": "once", "reason": "complete"}
    assert "owner-1" not in json.dumps(lines)


def test_main_one_shot_failure_exits_1(monkeypatch, capsys):
    briefings = StubService(exc=RuntimeError("boom"))
    alerts = StubService({"alerts_triggered": 0})
    _patch_main_deps(monkeypatch, briefings, alerts, ["tick"])
    with pytest.raises(SystemExit) as exc_info:
        tick.main()
    assert exc_info.value.code == 1
    lines = _json_lines(capsys)
    assert {"error": "briefings_tick_failed"} in lines
    assert lines[-1]["event"] == "tick_shutdown"


def test_main_loop_shutdown_logs_signal_reason(monkeypatch, capsys):
    briefings = StubService({"generated": 1})
    alerts = StubService({"alerts_triggered": 0})
    captured = _patch_main_deps(monkeypatch, briefings, alerts, ["tick", "--loop"])
    orig_run_once = tick.run_once

    def _run_once_and_stop(services, uid, shutdown=None):
        ok = orig_run_once(services, uid, shutdown)
        assert shutdown is captured["event"]
        shutdown.set()
        return ok

    monkeypatch.setattr(tick, "run_once", _run_once_and_stop)
    tick.main()
    lines = _json_lines(capsys)
    assert lines[0]["event"] == "tick_startup"
    assert lines[0]["mode"] == "loop"
    assert lines[-1] == {"event": "tick_shutdown", "mode": "loop", "reason": "signal"}
