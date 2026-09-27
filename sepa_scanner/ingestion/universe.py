"""Provider-master sync and active scan-universe management."""

from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

from config.settings import Settings, get_settings
from sepa_scanner.ingestion.providers.base import DataProvider
from sepa_scanner.storage.db import initialize_analytics


def sync_provider_instruments(provider: DataProvider) -> int:
    """Bulk-upsert NSE cash-equity mappings without changing the active scan scope."""
    instruments = [
        item for item in provider.list_instruments(exchange="NSE")
        if item.exchange == "NSE" and item.instrument_type == "EQ"
    ]
    if not instruments:
        return 0

    refreshed_at = datetime.now(timezone.utc)
    mappings = pd.DataFrame(
        [
            {
                "provider": provider.name,
                "symbol": item.symbol,
                "exchange": item.exchange,
                "provider_instrument_id": item.provider_instrument_id,
                "segment": item.segment,
                "instrument_type": item.instrument_type,
                "isin": item.isin,
                "name": item.name,
                "refreshed_at": refreshed_at,
            }
            for item in instruments
        ]
    )
    canonical = mappings[["symbol", "isin"]].drop_duplicates(subset=["symbol"])
    connection = initialize_analytics()
    try:
        connection.register("staged_provider_instruments", mappings)
        connection.execute(
            """
            INSERT INTO provider_instruments AS target
            SELECT * FROM staged_provider_instruments
            ON CONFLICT (provider, symbol, exchange) DO UPDATE SET
              provider_instrument_id = excluded.provider_instrument_id,
              segment = excluded.segment,
              instrument_type = excluded.instrument_type,
              isin = excluded.isin,
              name = excluded.name,
              refreshed_at = excluded.refreshed_at
            """
        )
        connection.register("staged_universe", canonical)
        connection.execute(
            """
            INSERT INTO universe AS target (symbol, isin, is_active)
            SELECT symbol, isin, FALSE FROM staged_universe
            ON CONFLICT (symbol) DO UPDATE SET
              isin = COALESCE(excluded.isin, target.isin)
            """
        )
        return len(instruments)
    finally:
        connection.close()


def sync_listing_dates(settings: Settings | None = None) -> dict[str, int | str]:
    """Refresh NSE listing dates by canonical symbol from NSE's equity master."""
    runtime_settings = settings or get_settings()
    source_url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    response = requests.get(
        source_url,
        headers={"User-Agent": "SEPA-Scanner/0.1 (personal research)"},
        timeout=30,
    )
    response.raise_for_status()
    frame = pd.read_csv(StringIO(response.text), skipinitialspace=True, dtype=str)
    required_columns = {"SYMBOL", "SERIES", "DATE OF LISTING"}
    if not required_columns.issubset(frame.columns):
        raise ValueError(
            f"NSE equity master is missing columns: {sorted(required_columns - set(frame.columns))}"
        )
    frame = frame.loc[frame["SERIES"].isin(["EQ", "BE"]), ["SYMBOL", "DATE OF LISTING"]]
    frame["symbol"] = frame["SYMBOL"].str.strip().str.upper()
    frame["listing_date"] = pd.to_datetime(
        frame["DATE OF LISTING"].str.strip(), format="%d-%b-%Y", errors="coerce"
    ).dt.date
    if frame.empty or frame["symbol"].duplicated().any() or frame["listing_date"].isna().any():
        raise ValueError("NSE equity master has duplicate symbols or invalid listing dates")
    staged = frame[["symbol", "listing_date"]].copy()

    archive_dir = runtime_settings.data_directory / "raw" / "universe"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"nse-equity-master-{date.today().isoformat()}.csv"
    archive_path.write_bytes(response.content)

    connection = initialize_analytics()
    try:
        connection.register("staged_listing_dates", staged)
        matched_symbols = connection.execute(
            """SELECT count(*) FROM universe target
               JOIN staged_listing_dates source USING (symbol)"""
        ).fetchone()[0]
        updated_symbols = connection.execute(
            """SELECT count(*) FROM universe target
               JOIN staged_listing_dates source USING (symbol)
               WHERE target.listing_date IS DISTINCT FROM source.listing_date"""
        ).fetchone()[0]
        connection.execute(
            """UPDATE universe AS target SET listing_date = source.listing_date
               FROM staged_listing_dates AS source
               WHERE target.symbol = source.symbol
                 AND target.listing_date IS DISTINCT FROM source.listing_date"""
        )
        remaining_unmatched = connection.execute(
            """SELECT count(*) FROM universe WHERE listing_date IS NULL"""
        ).fetchone()[0]
    finally:
        connection.close()

    return {
        "source_url": source_url,
        "source_rows": len(frame),
        "matched_symbols": matched_symbols,
        "updated_symbols": updated_symbols,
        "unmatched_universe_symbols": remaining_unmatched,
        "archive_path": str(archive_path),
    }


def _download_nifty500(settings: Settings) -> tuple[set[str], str]:
    """Download and archive the official Nifty 500 constituent CSV."""
    response = requests.get(
        settings.nifty500_constituents_url,
        headers={"User-Agent": "SEPA-Scanner/0.1 (personal research)"},
        timeout=30,
    )
    response.raise_for_status()
    frame = pd.read_csv(StringIO(response.text))
    if "Symbol" not in frame.columns:
        raise ValueError("Nifty 500 file does not contain the expected Symbol column")
    symbols = {str(symbol).strip().upper() for symbol in frame["Symbol"].dropna()}
    if not 450 <= len(symbols) <= 550:
        raise ValueError(f"Expected roughly 500 Nifty constituents; received {len(symbols)}")

    archive_dir = settings.data_directory / "raw" / "universe"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"nifty500-{date.today().isoformat()}.csv"
    archive_path.write_bytes(response.content)
    return symbols, settings.nifty500_constituents_url


def _load_custom_universe(settings: Settings) -> tuple[set[str], str | None]:
    """Load one symbol per line from a user-managed custom-universe file."""
    if not settings.custom_universe_path:
        raise ValueError("CUSTOM_UNIVERSE_PATH is required when UNIVERSE_SCOPE=custom")
    source_path = Path(settings.custom_universe_path)
    symbols = {
        line.strip().upper()
        for line in source_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not symbols:
        raise ValueError("Custom universe file contains no symbols")
    return symbols, str(source_path)


def apply_active_universe(
    provider: DataProvider, settings: Settings | None = None
) -> dict[str, int | str]:
    """Set the active scan universe selected in settings and store its snapshot."""
    runtime_settings = settings or get_settings()
    if runtime_settings.universe_scope == "nifty500":
        symbols, source_url = _download_nifty500(runtime_settings)
    elif runtime_settings.universe_scope == "custom":
        symbols, source_url = _load_custom_universe(runtime_settings)
    else:
        connection = initialize_analytics()
        try:
            rows = connection.execute(
                "SELECT symbol FROM provider_instruments WHERE provider = ? AND exchange = 'NSE'",
                [provider.name],
            ).fetchall()
        finally:
            connection.close()
        symbols = {row[0] for row in rows}
        source_url = None

    staged = pd.DataFrame({"symbol": sorted(symbols)})
    now = datetime.now(timezone.utc)
    today = date.today()
    connection = initialize_analytics()
    try:
        connection.register("staged_active_symbols", staged)
        connection.execute("UPDATE universe SET is_active = FALSE")
        connection.execute(
            """
            UPDATE universe AS target SET is_active = TRUE
            FROM staged_active_symbols AS source
            WHERE target.symbol = source.symbol
            """
        )
        connection.execute(
            """
            INSERT INTO universe_memberships
            SELECT ?, symbol, ?, ?, ? FROM staged_active_symbols
            ON CONFLICT DO NOTHING
            """,
            [runtime_settings.universe_scope, today, source_url, now],
        )
        active_count = connection.execute(
            "SELECT count(*) FROM universe WHERE is_active = TRUE"
        ).fetchone()[0]
    finally:
        connection.close()

    return {
        "scope": runtime_settings.universe_scope,
        "requested_symbols": len(symbols),
        "active_symbols": active_count,
    }
