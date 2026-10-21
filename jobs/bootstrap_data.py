"""Collect real completed Yahoo sessions; default mode persists them in Firestore."""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from app.catalog import INSTRUMENTS, get_instrument
from app.config import get_settings
from app.errors import AppError
from app.firebase import get_firestore
from app.providers.yahoo import YahooProvider
from app.services.market import MarketService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--instruments", nargs="+", choices=list(INSTRUMENTS), default=list(INSTRUMENTS)
    )
    parser.add_argument(
        "--start", type=date.fromisoformat, default=date.today() - timedelta(days=365)
    )
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--min-points", type=int, default=100)
    parser.add_argument(
        "--fetch-only", action="store_true", help="실제 수집만 점검; DB에 쓰지 않음"
    )
    parser.add_argument("--output", type=Path, help="fetch-only 실제 응답 저장 경로")
    args = parser.parse_args()
    if args.end < args.start or (args.end - args.start).days > 730 or args.min_points < 1:
        parser.error("기간은 최대 2년이며 min-points는 1 이상입니다.")
    if args.output and not args.fetch_only:
        parser.error("--output은 --fetch-only에서만 사용합니다.")
    settings = get_settings()
    if not args.fetch_only and (
        not settings.job_owner_uid or settings.job_owner_uid not in settings.allowed_uids
    ):
        parser.error("JOB_OWNER_UID를 ALLOWED_USER_UIDS에 포함해 설정하세요.")
    db = None if args.fetch_only else get_firestore()
    results, captured, failures = [], {}, []
    for instrument_id in args.instruments:
        try:
            if args.fetch_only:
                bars = YahooProvider().fetch_daily(
                    get_instrument(instrument_id), args.start, args.end
                )
                if len(bars) < args.min_points:
                    raise AppError(422, "insufficient_observations", "최소 관측치 수에 미달합니다.")
                captured[instrument_id] = bars
                result = {
                    "instrument_id": instrument_id,
                    "observations": len(bars),
                    "start_date": bars[0]["date"],
                    "end_date": bars[-1]["date"],
                    "provider": "yahoo",
                    "stored": False,
                }
            else:
                result = MarketService(db).import_history(
                    settings.job_owner_uid, instrument_id, args.start, args.end, args.min_points
                )
            results.append(result)
        except AppError as exc:
            failures.append(
                {"instrument_id": instrument_id, "code": exc.code, "message": exc.message}
            )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(captured, ensure_ascii=False, default=str), encoding="utf-8"
        )
    print(
        json.dumps(
            {"results": results, "failures": failures}, ensure_ascii=False, default=str, indent=2
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
