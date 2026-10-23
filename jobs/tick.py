"""One durable scheduler/outbox tick, or a local foreground loop. No API timer."""

import argparse
import json
import signal
import threading

from app.config import get_settings
from app.errors import AppError
from app.firebase import get_firestore
from app.services.alerts import AlertService
from app.services.briefings import BriefingService


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="현재 프로세스에서 주기 실행")
    parser.add_argument("--interval", type=int, default=60, help="loop 간격(초), 10~3600")
    args = parser.parse_args(argv)
    if not 10 <= args.interval <= 3600:
        parser.error("interval은 10~3600초여야 합니다.")
    return args


def _log(payload):
    print(json.dumps(payload), flush=True)


def _install_shutdown_handlers(shutdown):
    """Map SIGTERM/SIGINT to setting shutdown; handlers do no I/O."""

    def _request_shutdown(_signum, _frame):
        shutdown.set()

    installed = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            installed[sig] = signal.signal(sig, _request_shutdown)
        except (ValueError, OSError, RuntimeError):
            # Non-main thread or platform without this signal: callers
            # still exit via KeyboardInterrupt handling in run_*.
            continue
    return installed


def run_once(services, uid, shutdown=None):
    """Run each service tick once; a failure never suppresses the other job."""
    failed = False
    for name, service in services.items():
        if shutdown is not None and shutdown.is_set():
            break
        try:
            _log({name: service.tick(uid)})
        except KeyboardInterrupt:
            if shutdown is not None:
                shutdown.set()
            failed = True
            break
        except Exception:
            # Omit arbitrary upstream text; one job cannot suppress the other.
            _log({"error": name + "_tick_failed"})
            failed = True
    return failed


def run_loop(services, uid, interval, shutdown):
    """Loop until shutdown; idle wait ends promptly once shutdown is set."""
    while not shutdown.is_set():
        run_once(services, uid, shutdown)
        if shutdown.is_set():
            break
        try:
            if shutdown.wait(interval):
                break
        except KeyboardInterrupt:
            shutdown.set()
            break


def main():
    args = parse_args()
    settings = get_settings()
    uid = settings.job_owner_uid
    if not uid or uid not in settings.allowed_uids:
        raise AppError(503, "configuration_missing", "허용 목록에 있는 JOB_OWNER_UID가 필요합니다.")
    db = get_firestore()
    services = {"briefings": BriefingService(db, settings), "alerts": AlertService(db, settings)}
    shutdown = threading.Event()
    _install_shutdown_handlers(shutdown)
    mode = "loop" if args.loop else "once"
    _log({"event": "tick_startup", "mode": mode, "interval": args.interval})
    try:
        if not args.loop:
            failed = run_once(services, uid, shutdown)
            reason = "signal" if shutdown.is_set() else "complete"
            _log({"event": "tick_shutdown", "mode": mode, "reason": reason})
            if failed:
                raise SystemExit(1)
            return
        run_loop(services, uid, args.interval, shutdown)
        reason = "signal" if shutdown.is_set() else "complete"
        _log({"event": "tick_shutdown", "mode": mode, "reason": reason})
    except KeyboardInterrupt:
        shutdown.set()
        _log({"event": "tick_shutdown", "mode": mode, "reason": "signal"})
        return


if __name__ == "__main__":
    main()
