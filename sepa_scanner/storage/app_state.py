"""SQLite connection helper for single-user application state."""

import sqlite3
from pathlib import Path


def connect_app_state(path: str | Path = "data/app_state.sqlite") -> sqlite3.Connection:
    """Open the local SQLite database and ensure its parent directory exists."""
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection
