from datetime import date, timedelta

from app.catalog import get_instrument
from app.domain.summary import summarize
from app.errors import AppError
from app.repositories.data import DataRepository


def date_range(start_date=None, end_date=None):
    end = end_date or date.today()
    start = start_date or end - timedelta(days=365)
    if end < start or (end - start).days > 730:
        raise AppError(422, "invalid_date_range", "기간은 시작일 이상, 최대 2년이어야 합니다.")
    return start, end


class DataService:
    def __init__(self, db):
        self.repository = DataRepository(db)

    def create(self, uid, payload):
        get_instrument(payload.instrument_id)
        return self.repository.create(uid, payload.model_dump(mode="json"))

    def update(self, uid, doc_id, payload):
        return self.repository.update(uid, doc_id, payload.model_dump(mode="json"))

    def delete(self, uid, doc_id):
        self.repository.delete(uid, doc_id)

    def list(self, uid, instrument_id, start_date=None, end_date=None, limit=50, cursor=None):
        get_instrument(instrument_id)
        start, end = date_range(start_date, end_date)
        return self.repository.list(uid, instrument_id, start, end, limit, cursor)

    def summary(self, uid, instrument_id, start_date=None, end_date=None):
        instrument = get_instrument(instrument_id)
        start, end = date_range(start_date, end_date)
        result = summarize(self.repository.all(uid, instrument_id, start, end), instrument)
        return {
            **result,
            "requested_start_date": start.isoformat(),
            "requested_end_date": end.isoformat(),
        }
