"""Provider-neutral market data contracts."""

from dataclasses import dataclass
from datetime import date
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class DailyBar:
    """A normalized daily OHLCV record returned by any provider."""

    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    adj_close: float | None = None
    delivery_pct: float | None = None


@dataclass(frozen=True, slots=True)
class Instrument:
    """Normalized broker instrument metadata used to resolve a ticker."""

    symbol: str
    provider_instrument_id: str
    exchange: str
    segment: str
    instrument_type: str
    isin: str | None = None
    name: str | None = None


class DataProvider(Protocol):
    """Contract implemented by Kite, Dhan, Upstox, and future adapters."""

    name: str

    def list_instruments(self, exchange: str = "NSE") -> Sequence[Instrument]:
        """Return tradable instruments from the provider's instrument master."""

    def fetch_daily(self, instrument: Instrument, start: date, end: date) -> Sequence[DailyBar]:
        """Fetch normalized daily bars for an inclusive date range."""
