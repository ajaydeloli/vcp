"""Resumable, bounded historical-backfill orchestration."""

from datetime import date

from config.settings import Settings, get_settings
from sepa_scanner.ingestion.daily_update import run_daily_update
from sepa_scanner.ingestion.providers.base import DataProvider
from sepa_scanner.storage.db import initialize_analytics


def pending_backfill_symbols(provider: DataProvider) -> list[str]:
    """Return active symbols that have no local daily history yet."""
    connection = initialize_analytics()
    try:
        rows = connection.execute(
            """
            SELECT universe.symbol
            FROM universe
            JOIN provider_instruments provider_instrument
              ON provider_instrument.symbol = universe.symbol
            LEFT JOIN ohlcv_daily daily ON daily.symbol = universe.symbol
            WHERE universe.is_active = TRUE
              AND provider_instrument.provider = ?
              AND provider_instrument.exchange = 'NSE'
            GROUP BY universe.symbol
            HAVING count(daily.date) = 0
               OR max(daily.date) < (SELECT max(date) FROM ohlcv_daily)
            ORDER BY universe.symbol
            """,
            [provider.name],
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        connection.close()


def run_backfill_batch(
    provider: DataProvider,
    *,
    batch_size: int = 25,
    settings: Settings | None = None,
    initial_start_date: date = date(2018, 1, 1),
) -> dict[str, int | str]:
    """Backfill one deterministic batch, leaving later symbols for the next run."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least one")
    pending = pending_backfill_symbols(provider)
    selected = set(pending[:batch_size])
    if not selected:
        return {"symbols_selected": 0, "symbols_remaining": 0, "rows_added": 0}

    result = run_daily_update(
        provider,
        settings=settings or get_settings(),
        symbols=selected,
        initial_start_date=initial_start_date,
    )
    remaining = len(pending) - len(selected)
    return {**result, "symbols_selected": len(selected), "symbols_remaining": remaining}
