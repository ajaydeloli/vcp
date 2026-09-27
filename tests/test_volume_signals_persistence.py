"""Tests for the persisted, universe-wide volume-signals batch runner.

Uses a temporary DuckDB file (via Settings.data_directory) and the module's
*real* default config (config/volume_signals.yaml), rather than the small
custom configs used in test_volume_signals.py's pure-function unit tests --
this exercises the actual lookback windows (50/50/10+20 sessions) that
run_volume_signals() will use in production.
"""

from __future__ import annotations

import pandas as pd
import pytest

from config.settings import Settings
from sepa_scanner.analytics.volume_signals import latest_volume_signals, run_volume_signals
from sepa_scanner.storage.db import connect_analytics, initialize_analytics


def _uptrend_with_spike_and_pivot() -> tuple[pd.DataFrame, float]:
    """55 sessions: mostly up days (4 deliberate down days), every close
    sitting 60% up its own high/low range (deterministic rising A/D line
    regardless of price direction), and a clean volume spike on the final
    up day, priced exactly at the pivot.
    """
    closes = [100 + i * 0.5 for i in range(55)]
    for index in (10, 20, 30, 40):
        closes[index] = closes[index - 1] - 1.0
    closes[-1] = closes[-2] + 2.0  # a clean up day right at the end
    volumes = [100_000] * 55
    volumes[-1] = 500_000  # a clear volume spike on that final up day

    dates = pd.date_range("2024-01-01", periods=55, freq="D")
    bars = pd.DataFrame({
        "symbol": "VOLTEST",
        "date": dates,
        "open": closes,
        "high": [c + 0.2 for c in closes],
        "low": [c - 0.8 for c in closes],
        "close": closes,
        "volume": volumes,
        "delivery_pct": None,
        "adj_close": closes,
        "source": "test",
        "ingested_at": pd.Timestamp("2024-03-01"),
    })
    return bars, closes[-1]


@pytest.fixture()
def seeded(tmp_path):
    """Returns (settings, expected_pivot_price)."""
    settings = Settings(data_directory=tmp_path)
    bars, pivot_price = _uptrend_with_spike_and_pivot()
    connection = initialize_analytics(tmp_path / "market.duckdb")
    try:
        connection.execute("INSERT INTO universe (symbol, is_active) VALUES ('VOLTEST', TRUE)")
        connection.register("daily_seed", bars)
        connection.execute("INSERT INTO ohlcv_daily SELECT * FROM daily_seed")
        connection.unregister("daily_seed")
        connection.execute(
            """INSERT INTO vcp_results_weekly
               (symbol, date, is_vcp, quality_grade, reason, pivot_price, pivot_date,
                contraction_count, base_length_weeks, depth_decay_passed,
                volume_decay_passed, base_length_passed, daily_confirmation_available)
               VALUES ('VOLTEST', DATE '2024-02-25', TRUE, 'B', 'seeded for test',
                       ?, DATE '2024-02-25', 3, 16, TRUE, TRUE, TRUE, FALSE)""",
            [pivot_price],
        )
    finally:
        connection.close()
    return settings, pivot_price


def test_run_volume_signals_persists_expected_values(seeded):
    settings, pivot_price = seeded
    summary = run_volume_signals(settings=settings)

    assert summary["symbols_evaluated"] == 1
    assert summary["symbols_with_pivot_spike"] == 1

    connection = connect_analytics(settings.data_directory / "market.duckdb")
    try:
        row = connection.execute(
            "SELECT * FROM volume_signals_daily WHERE symbol = 'VOLTEST'"
        ).fetchdf()
    finally:
        connection.close()

    assert len(row) == 1
    record = row.iloc[0]
    # More up days than down days (4 deliberate down days out of 54), so the
    # up/down volume ratio should be finite and well above 1.
    assert record["up_down_volume_ratio"] > 1
    assert bool(record["accumulation_distribution_rising"]) is True
    assert record["pivot_price_used"] == pytest.approx(pivot_price)
    assert bool(record["volume_spike_near_pivot"]) is True
    assert record["spike_volume"] == 500_000


def test_run_volume_signals_is_idempotent_on_rerun(seeded):
    settings, _ = seeded
    run_volume_signals(settings=settings)
    summary = run_volume_signals(settings=settings)

    assert summary["symbols_evaluated"] == 1
    connection = connect_analytics(settings.data_directory / "market.duckdb")
    try:
        count = connection.execute(
            "SELECT count(*) FROM volume_signals_daily WHERE symbol = 'VOLTEST'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_latest_volume_signals_returns_the_persisted_row(seeded):
    settings, _ = seeded
    run_volume_signals(settings=settings)

    latest = latest_volume_signals(settings=settings)

    assert len(latest) == 1
    assert latest.iloc[0]["symbol"] == "VOLTEST"
