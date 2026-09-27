"""Tests for the persisted, universe-wide composite scoring batch runner.

Uses a temporary DuckDB file (via Settings.data_directory) so this never
touches the real market.duckdb. The Trend Template is exercised against
real seeded daily bars (a clean uptrend that clears every criterion); the
other four components are hand-seeded directly into their own tables, the
same way test_volume_signals_persistence.py seeds vcp_results_weekly
rather than re-running the full upstream pipeline.
"""

from __future__ import annotations

import pandas as pd
import pytest

from config.settings import Settings
from sepa_scanner.scoring.scorer import calculate_symbol_score, latest_scores, run_scoring
from sepa_scanner.storage.db import connect_analytics, initialize_analytics


def _rising_daily_bars(symbol: str) -> pd.DataFrame:
    """400 sessions of a steady, unbroken uptrend -- comfortably clears every
    Trend Template criterion (MAs stacked bullishly, price at the 52-week
    high, ~46% above the 52-week low) without needing a hand-tuned fixture."""
    sessions = 400
    prices = [100 * (1.0015**i) for i in range(sessions)]
    dates = pd.bdate_range("2023-01-02", periods=sessions)
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": dates,
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1_000_000] * sessions,
            "delivery_pct": None,
            "adj_close": prices,
            "source": "test",
            "ingested_at": pd.Timestamp("2024-06-01"),
        }
    )


@pytest.fixture()
def seeded_settings(tmp_path):
    """A gate-passing symbol (SCORETEST) with real daily bars for the trend
    template, plus hand-seeded rows in every other analytics table the
    scorer reads."""
    settings = Settings(data_directory=tmp_path)
    bars = _rising_daily_bars("SCORETEST")
    connection = initialize_analytics(tmp_path / "market.duckdb")
    try:
        connection.execute("INSERT INTO universe (symbol, is_active) VALUES ('SCORETEST', TRUE)")
        connection.register("daily_seed", bars)
        connection.execute("INSERT INTO ohlcv_daily SELECT * FROM daily_seed")
        connection.unregister("daily_seed")
        last_date = bars["date"].iloc[-1].date()

        connection.execute(
            "INSERT INTO rs_ratings_daily VALUES ('SCORETEST', ?, 0.4, 85, 1.2, TRUE, 500)",
            [last_date],
        )
        connection.execute(
            "INSERT INTO market_stages_daily VALUES ('SCORETEST', ?, 2, 149.0, 140.0, 130.0, 120.0, 0.05, 8)",
            [last_date],
        )
        connection.execute(
            """INSERT INTO vcp_results_weekly
               (symbol, date, is_vcp, quality_grade, reason, pivot_price, pivot_date,
                contraction_count, base_length_weeks, depth_decay_passed,
                volume_decay_passed, base_length_passed, daily_confirmation_available,
                near_pivot, tight_close_count, tight_closes_confirmed, breakout_confirmed)
               VALUES ('SCORETEST', ?, TRUE, 'A', 'seeded for test', 148.0, ?, 3, 16,
                       TRUE, TRUE, TRUE, TRUE, TRUE, 3, TRUE, TRUE)""",
            [last_date, last_date],
        )
        connection.execute(
            "INSERT INTO volume_signals_daily VALUES "
            "('SCORETEST', ?, 2.5, 100.0, 40.0, 5000.0, TRUE, 148.0, TRUE, ?, 500000)",
            [last_date, last_date],
        )
    finally:
        connection.close()
    return settings


def test_run_scoring_persists_a_gate_passing_symbol(seeded_settings):
    summary = run_scoring(settings=seeded_settings)

    assert summary["symbols_scored"] == 1
    assert summary["symbols_gate_passed"] == 1

    connection = connect_analytics(seeded_settings.data_directory / "market.duckdb")
    try:
        row = connection.execute("SELECT * FROM scores_daily WHERE symbol = 'SCORETEST'").fetchdf()
    finally:
        connection.close()

    assert len(row) == 1
    record = row.iloc[0]
    assert bool(record["gate_passed"]) is True
    assert bool(record["gate_trend_template_passed"]) is True
    assert bool(record["gate_relative_strength_passed"]) is True
    assert record["composite_score"] > 50
    assert record["quality_grade"] == "A"
    assert record["stage"] == 2
    assert record["relative_strength_rating"] == 85
    assert bool(record["fundamentals_evaluated"]) is False
    # DuckDB NULL round-trips through fetchdf() as NaN, not None.
    assert pd.isna(record["fundamentals_score"])


def test_run_scoring_is_idempotent_on_rerun(seeded_settings):
    """Re-running the same day's batch must upsert, not duplicate rows --
    the project's stated idempotency requirement applies here too."""
    run_scoring(settings=seeded_settings)
    summary = run_scoring(settings=seeded_settings)

    assert summary["symbols_scored"] == 1
    connection = connect_analytics(seeded_settings.data_directory / "market.duckdb")
    try:
        count = connection.execute(
            "SELECT count(*) FROM scores_daily WHERE symbol = 'SCORETEST'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_latest_scores_returns_the_persisted_row(seeded_settings):
    run_scoring(settings=seeded_settings)

    latest = latest_scores(settings=seeded_settings)

    assert len(latest) == 1
    assert latest.iloc[0]["symbol"] == "SCORETEST"
    assert bool(latest.iloc[0]["gate_passed"]) is True


def test_calculate_symbol_score_matches_run_scoring_without_a_prior_batch_run(seeded_settings):
    """The on-demand single-symbol path should work even if run_scoring()
    was never called -- it recomputes the Trend Template live."""
    result = calculate_symbol_score("SCORETEST", settings=seeded_settings)

    assert result["symbol"] == "SCORETEST"
    assert result["gate_passed"] is True
    assert result["stage"] == 2
    assert result["relative_strength_rating"] == 85
