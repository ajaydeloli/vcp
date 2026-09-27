"""Kite Connect market-data adapter."""

from datetime import date, datetime, timedelta
from time import sleep
from typing import Any, Sequence

from kiteconnect import KiteConnect

from config.settings import Settings
from sepa_scanner.ingestion.providers.base import DailyBar, Instrument


class KiteDataProvider:
    """Fetch NSE instrument and daily candle data through Kite Connect."""

    name = "kite"

    def __init__(self, settings: Settings, client: KiteConnect | None = None) -> None:
        if not settings.kite.api_key or not settings.kite.access_token:
            raise ValueError("KITE__API_KEY and KITE__ACCESS_TOKEN must be configured")
        self._max_historical_days = settings.kite.max_historical_days
        self._request_delay_seconds = settings.kite.request_delay_seconds
        self._max_retries = settings.kite.max_retries
        self._client = client or KiteConnect(api_key=settings.kite.api_key)
        self._client.set_access_token(settings.kite.access_token)

    def list_instruments(self, exchange: str = "NSE") -> Sequence[Instrument]:
        """Return cash-equity instruments for the requested exchange."""
        rows: list[dict[str, Any]] = self._client.instruments(exchange=exchange)
        return [
            Instrument(
                symbol=row["tradingsymbol"],
                provider_instrument_id=str(row["instrument_token"]),
                exchange=row["exchange"],
                segment=row["segment"],
                instrument_type=row["instrument_type"],
                isin=row.get("isin") or None,
                name=row.get("name") or None,
            )
            for row in rows
            if row["exchange"] == exchange and row["instrument_type"] == "EQ"
        ]

    def fetch_daily(self, instrument: Instrument, start: date, end: date) -> Sequence[DailyBar]:
        """Fetch a date range in paced, retryable API-sized chunks."""
        if end < start:
            raise ValueError("end date must be on or after start date")

        candles: list[DailyBar] = []
        chunk_start = start
        first_request = True
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=self._max_historical_days - 1), end)
            if not first_request:
                sleep(self._request_delay_seconds)
            rows = self._fetch_chunk(instrument, chunk_start, chunk_end)
            candles.extend(self._normalise_candle(row) for row in rows)
            chunk_start = chunk_end + timedelta(days=1)
            first_request = False
        return candles

    def _fetch_chunk(
        self, instrument: Instrument, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Retry a single historical request with small exponential backoff."""
        for attempt in range(self._max_retries):
            try:
                return self._client.historical_data(
                    instrument_token=int(instrument.provider_instrument_id),
                    from_date=start.isoformat(),
                    to_date=end.isoformat(),
                    interval="day",
                )
            except Exception:
                if attempt == self._max_retries - 1:
                    raise
                sleep(self._request_delay_seconds * (2**attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _normalise_candle(row: dict[str, Any]) -> DailyBar:
        candle_date = row["date"]
        if isinstance(candle_date, datetime):
            candle_date = candle_date.date()
        elif not isinstance(candle_date, date):
            candle_date = date.fromisoformat(str(candle_date)[:10])
        return DailyBar(
            date=candle_date,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
        )
