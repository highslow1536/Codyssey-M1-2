from app.catalog import get_instrument
from app.errors import AppError
from app.providers.yahoo import YahooProvider
from app.repositories.market import MarketRepository
from app.services.data import date_range


class MarketService:
    def __init__(self, db, settings=None, provider=None):
        self.repository = MarketRepository(db)
        self.provider = provider or YahooProvider()

    def import_history(self, uid, instrument_id, start_date, end_date, min_points=0):
        instrument = get_instrument(instrument_id)
        start, end = date_range(start_date, end_date)
        bars = self.provider.fetch_daily(instrument, start, end)
        if len(bars) < min_points:
            raise AppError(
                422,
                "insufficient_observations",
                f"실제 관측치 {len(bars)}건입니다. 필요한 최소 {min_points}건보다 적습니다.",
            )
        counts = self.repository.save_bars(uid, instrument, bars)
        return {
            "instrument_id": instrument_id,
            "provider": "yahoo",
            "start_date": bars[0]["date"],
            "end_date": bars[-1]["date"],
            "price_basis": "adjusted_close",
            **counts,
        }

    def history(self, instrument_id, start_date=None, end_date=None):
        instrument = get_instrument(instrument_id)
        start, end = date_range(start_date, end_date)
        items = self.repository.history(instrument_id, start, end)
        return {
            "instrument": instrument,
            "items": items,
            "quality_status": "ok" if items else "unavailable",
        }
