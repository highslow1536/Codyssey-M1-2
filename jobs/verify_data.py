"""Verify the actual stored dataset; optionally write a local verification report."""

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.catalog import INSTRUMENTS, get_instrument
from app.config import get_settings
from app.errors import AppError
from app.firebase import get_firestore
from app.repositories.market import MarketRepository
from app.services.data import DataService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check-reimport", action="store_true", help="저장된 실제 원본을 다시 복사해 중복 검사"
    )
    args = parser.parse_args()
    settings = get_settings()
    uid = settings.job_owner_uid
    if not uid or uid not in settings.allowed_uids:
        parser.error("허용 목록의 JOB_OWNER_UID를 설정하세요.")
    db = get_firestore()
    service, market = DataService(db), MarketRepository(db)
    start, end = date.today() - timedelta(days=365), date.today()
    results = []
    for instrument_id in INSTRUMENTS:
        summary = service.summary(uid, instrument_id, start, end)
        rows = market.history(instrument_id, start, end)
        if summary["count"] < 100 or summary["unique_dates"] < 100 or len(rows) < 100:
            raise AppError(
                422, "insufficient_observations", f"{instrument_id}: 실제 관측치가 부족합니다."
            )
        if (
            summary["source_counts"] != {"yahoo": summary["count"]}
            or summary["user_modified_count"]
        ):
            raise AppError(
                422, "dataset_modified", f"{instrument_id}: 원본 복사 데이터만 검증합니다."
            )
        result = {
            "instrument_id": instrument_id,
            "count": summary["count"],
            "start_date": summary["start_date"],
            "end_date": summary["end_date"],
            "source_urls": summary["source_urls"],
            "data_as_of": summary["data_as_of"],
        }
        if args.check_reimport:
            bars = [
                {k: v for k, v in row.items() if k not in {"id", "revision", "source_fingerprint"}}
                for row in rows
            ]
            counts = market.save_bars(uid, get_instrument(instrument_id), bars)
            if counts["data_created"] or counts["data_updated"] or counts["market_updated"]:
                raise AppError(
                    409, "reimport_changed", f"{instrument_id}: 재가져오기 결과를 확인하세요."
                )
            result["duplicate_data_created"] = counts["data_created"]
        results.append(result)
    report = {
        "verified_at": datetime.now(timezone.utc),
        "storage": "firestore_emulator" if settings.firestore_emulator_host else "cloud_firestore",
        "total_observations": sum(row["count"] for row in results),
        "instruments": results,
    }
    serialized = json.dumps(report, default=str, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
