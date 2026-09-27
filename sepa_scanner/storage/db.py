"""DuckDB analytical storage setup and connection helpers."""

from pathlib import Path

import duckdb

from config.settings import Settings, get_settings


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect_analytics(path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open the local DuckDB analytical store."""
    database_path = Path(path) if path else get_settings().data_directory / "market.duckdb"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(database_path))


def initialize_analytics(path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Create all analytical tables if this is a new local data store."""
    connection = connect_analytics(path)
    connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    return connection
