"""Tests for the persisted, universe-wide VCP batch runner.

Uses a temporary DuckDB file (via Settings.data_directory) so this never
touches the real market.duckdb, and reuses the same textbook contracting
pattern verified in test_vcp_detector.py.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from config.settings import Settings
from sepa_scanner.analytics.vcp_detector import latest_vcp_results, run_vcp_detection
from sepa_scanner.storage.db import connect_analytics, initialize_analytics


def _textbook_prices_and_volumes() -> tuple[list[float], list[int]]:
    """Same fixture as test_vcp_detector.py's textbook VCP (see that file for
    the hand-verified swing-detection trace)."""
    prices = [
        100, 105, 110, 115, 120, 125, 130,  # Peak 1 (index 6)
        122, 113, 104,  # Trough 1 (index 9) -- depth 20%
        111, 118, 124,  # Peak 2 (index 12)
        118, 114, 111.6,  # Trough 2 (index 15) -- depth 10%
        115, 118, 120,  # Peak 3 (index 18)
        118, 116, 114,  # Trough 3 (index 21) -- depth 5%
        114.5, 115.5,
    ]
    volumes = [1_000_000] * len(prices)
    for index in (6, 7, 8, 9):
        volumes[index] = 2_000_000
    for index in (18, 19, 20, 21):
        volumes[index] = 500_000
    return prices, volumes


@pytest.fixture()
def seeded_settings(tmp_path):
    """A Settings instance pointed at a throwaway DuckDB file, pre-seeded with
    one active symbol carrying the textbook contracting weekly bars."""
    settings = Settings(data_directory=tmp_path)
    connection = initialize_analytics(tmp_path / "market.duckdb")
    try:
        connection.execute(
            "INSERT INTO universe (symbol, is_active) VALUES ('TEXTBOOK', TRUE)"
        )
        prices, volumes = _textbook_prices_and_volumes()
        dates = pd.date_range("2024-01-07", periods=len(prices), freq="7D")
        weekly = pd.DataFrame(
            {
                "symbol": "TEXTBOOK",
                "week_end_date": dates,
                "open": prices,
                "high": prices,
                "low": prices,
                "close": prices,
                "volume": volumes,
                "adj_close": prices,
            }
        )
        connection.register("weekly_seed", weekly)
        connection.execute("INSERT INTO ohlcv_weekly SELECT * FROM weekly_seed")
        connection.unregister("weekly_seed")
    finally:
        connection.close()
    return settings


def test_run_vcp_detection_persists_a_qualifying_symbol(seeded_settings):
    summary = run_vcp_detection(settings=seeded_settings)

    assert summary["symbols_evaluated"] == 1
    assert summary["symbols_flagged_vcp"] == 1

    connection = connect_analytics(seeded_settings.data_directory / "market.duckdb")
    try:
        row = connection.execute(
            "SELECT * FROM vcp_results_weekly WHERE symbol = 'TEXTBOOK'"
        ).fetchdf()
    finally:
        connection.close()

    assert len(row) == 1
    record = row.iloc[0]
    assert bool(record["is_vcp"]) is True
    assert record["quality_grade"] == "B"
    assert record["contraction_count"] == 3
    assert record["pivot_price"] == 120

    contractions = json.loads(record["contractions_json"])
    assert len(contractions) == 3


def test_run_vcp_detection_is_idempotent_on_rerun(seeded_settings):
    """Re-running the same day's batch must upsert, not duplicate rows --
    the project's stated idempotency requirement applies here too."""
    run_vcp_detection(settings=seeded_settings)
    summary = run_vcp_detection(settings=seeded_settings)

    assert summary["symbols_evaluated"] == 1
    connection = connect_analytics(seeded_settings.data_directory / "market.duckdb")
    try:
        count = connection.execute(
            "SELECT count(*) FROM vcp_results_weekly WHERE symbol = 'TEXTBOOK'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_latest_vcp_results_returns_the_persisted_row(seeded_settings):
    run_vcp_detection(settings=seeded_settings)

    latest = latest_vcp_results(settings=seeded_settings)

    assert len(latest) == 1
    assert latest.iloc[0]["symbol"] == "TEXTBOOK"
    assert bool(latest.iloc[0]["is_vcp"]) is True
