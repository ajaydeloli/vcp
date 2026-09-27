"""Idempotent incremental daily OHLCV ingestion."""

import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

from config.settings import Settings, get_settings
from sepa_scanner.ingestion.providers.base import DataProvider, Instrument
from sepa_scanner.storage.db import initialize_analytics


def _validate_bar(symbol: str, bar: object) -> None:
    """Reject impossible OHLCV data before it reaches the analytical store."""
    open_price = bar.open
    high = bar.high
    low = bar.low
    close = bar.close
    if min(open_price, high, low, close) < 0:
        raise ValueError(f"{symbol}: prices cannot be negative")
    if low > min(open_price, close) or high < max(open_price, close):
        raise ValueError(f"{symbol}: OHLC values are inconsistent")
    if bar.volume < 0:
        raise ValueError(f"{symbol}: volume cannot be negative")


def _resample_weekly(connection: object, symbols: list[str]) -> None:
    """Derive weekly bars from stored daily data for updated symbols only."""
    if not symbols:
        return
    connection.execute(
        """
        DELETE FROM ohlcv_weekly WHERE symbol IN (SELECT * FROM UNNEST(?))
        """,
        [symbols],
    )
    connection.execute(
        """
        INSERT INTO ohlcv_weekly
        SELECT
          symbol,
          CAST(date_trunc('week', date) + INTERVAL 4 DAY AS DATE) AS week_end_date,
          arg_min(open, date) AS open,
          max(high) AS high,
          min(low) AS low,
          arg_max(close, date) AS close,
          sum(volume) AS volume,
          arg_max(adj_close, date) AS adj_close
        FROM ohlcv_daily
        WHERE symbol IN (SELECT * FROM UNNEST(?))
        GROUP BY symbol, date_trunc('week', date)
        """,
        [symbols],
    )


def run_daily_update(
    provider: DataProvider,
    *,
    settings: Settings | None = None,
    end_date: date | None = None,
    symbols: set[str] | None = None,
    initial_start_date: date = date(2018, 1, 1),
) -> dict[str, int | str]:
    """Fetch missing dates for all active symbols or a controlled symbol subset."""
    runtime_settings = settings or get_settings()
    requested_symbols = symbols
    run_id = str(uuid4())
    now = datetime.now(timezone.utc)
    latest_date = end_date or date.today()
    connection = initialize_analytics()
    raw_directory = runtime_settings.data_directory / "raw" / run_id
    updated_symbols: list[str] = []
    failed_symbols = 0
    failure_notes: list[str] = []
    rows_added = 0

    try:
        instrument_rows = connection.execute(
            """
            SELECT p.symbol, p.provider_instrument_id, p.exchange, p.segment, p.instrument_type,
                   p.isin, p.name
            FROM provider_instruments p
            JOIN universe u USING (symbol)
            WHERE p.provider = ? AND p.exchange = 'NSE' AND u.is_active = TRUE
            ORDER BY p.symbol
            """,
            [provider.name],
        ).fetchall()
        if requested_symbols is not None:
            instrument_rows = [row for row in instrument_rows if row[0] in requested_symbols]
        for row in instrument_rows:
            instrument = Instrument(
                symbol=row[0], provider_instrument_id=row[1], exchange=row[2], segment=row[3],
                instrument_type=row[4], isin=row[5], name=row[6],
            )
            last_stored = connection.execute(
                "SELECT max(date) FROM ohlcv_daily WHERE symbol = ?", [instrument.symbol]
            ).fetchone()[0]
            start_date = (
                last_stored + timedelta(days=1)
                if last_stored
                else initial_start_date
            )
            if start_date > latest_date:
                continue
            try:
                bars = list(provider.fetch_daily(instrument, start_date, latest_date))
                for bar in bars:
                    _validate_bar(instrument.symbol, bar)
                if not bars:
                    continue
                raw_directory.mkdir(parents=True, exist_ok=True)
                (raw_directory / f"{instrument.symbol}.json").write_text(
                    json.dumps([asdict(bar) | {"date": bar.date.isoformat()} for bar in bars]),
                    encoding="utf-8",
                )
                staged_bars = pd.DataFrame(
                    [
                        {
                            "symbol": instrument.symbol,
                            "date": bar.date,
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                            "delivery_pct": bar.delivery_pct,
                            "adj_close": bar.adj_close or bar.close,
                            "source": provider.name,
                            "ingested_at": now,
                        }
                        for bar in bars
                    ]
                )
                connection.register("staged_daily_bars", staged_bars)
                try:
                    connection.execute(
                        """
                        INSERT INTO ohlcv_daily AS target
                        SELECT * FROM staged_daily_bars
                        ON CONFLICT (symbol, date) DO UPDATE SET
                          open = excluded.open, high = excluded.high, low = excluded.low,
                          close = excluded.close, volume = excluded.volume,
                          delivery_pct = excluded.delivery_pct, adj_close = excluded.adj_close,
                          source = excluded.source, ingested_at = excluded.ingested_at
                        """
                    )
                finally:
                    connection.unregister("staged_daily_bars")
                rows_added += len(bars)
                updated_symbols.append(instrument.symbol)
            except Exception as error:
                failed_symbols += 1
                failure_notes.append(f"{instrument.symbol}: {type(error).__name__}: {str(error)[:160]}")
        resample_symbols = updated_symbols if requested_symbols is None else sorted(requested_symbols)
        _resample_weekly(connection, resample_symbols)
        connection.execute(
            "INSERT INTO ingestion_log VALUES (?, ?, ?, ?, ?, ?)",
            [
                run_id, now, len(updated_symbols), failed_symbols, rows_added,
                "controlled incremental update" + ("; " + " | ".join(failure_notes) if failure_notes else ""),
            ],
        )
    finally:
        connection.close()

    return {
        "run_id": run_id,
        "symbols_updated": len(updated_symbols),
        "symbols_failed": failed_symbols,
        "rows_added": rows_added,
    }
