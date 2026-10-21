"""One durable scheduler/outbox tick, or a local foreground loop. No API timer."""

import argparse
import json
import time

from app.config import get_settings
from app.errors import AppError
from app.firebase import get_firestore
from app.services.briefings import BriefingService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="현재 프로세스에서 주기 실행")
    parser.add_argument("--interval", type=int, default=60, help="loop 간격(초), 10~3600")
    args = parser.parse_args()
    if not 10 <= args.interval <= 3600:
        parser.error("interval은 10~3600초여야 합니다.")
    settings = get_settings()
    uid = settings.job_owner_uid
    if not uid or uid not in settings.allowed_uids:
        raise AppError(503, "configuration_missing", "허용 목록에 있는 JOB_OWNER_UID가 필요합니다.")
    service = BriefingService(get_firestore(), settings)
    while True:
        try:
            print(json.dumps(service.tick(uid)), flush=True)
        except Exception:
            # Deliberately omit arbitrary upstream exception text and credentials.
            print(json.dumps({"error": "briefing_tick_failed"}), flush=True)
            if not args.loop:
                raise SystemExit(1) from None
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
