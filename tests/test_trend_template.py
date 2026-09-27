"""Tests for sepa_scanner.analytics.trend_template.

Uses an explicit config dict (rather than config/trend_template.yaml) so these
tests stay correct even if the on-disk defaults are retuned later.
"""

from __future__ import annotations

import pandas as pd
import pytest

from sepa_scanner.analytics.trend_template import calculate_trend_template

CONFIG = {
    "periods": {
        "daily": {"short": 50, "medium": 150, "long": 200, "month": 21, "year": 252},
        "weekly": {"short": 10, "medium": 30, "long": 40, "month": 4, "year": 52},
    },
    "criteria": {
        "minimum_gain_from_52_week_low": 0.25,
        "maximum_decline_from_52_week_high": 0.25,
        "minimum_relative_strength_rating": 70,
    },
}


def _daily_bars(prices: list[float], start: str = "2023-01-02") -> pd.DataFrame:
    """Build a daily bars frame with high/low pinned close to the close price."""
    dates = pd.date_range(start=start, periods=len(prices), freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "close": prices,
            "high": [p * 1.001 for p in prices],
            "low": [p * 0.999 for p in prices],
        }
    )


def test_insufficient_history_marks_moving_average_criteria_unevaluated():
    """Fewer than 200 bars must leave every MA-dependent criterion as None, not failed."""
    bars = _daily_bars([100 + i for i in range(30)])

    result = calculate_trend_template(bars, "daily", relative_strength_rating=80, config=CONFIG)

    assert result["criteria"]["price_above_150_and_200_ma"]["passed"] is None
    assert result["criteria"]["ma_50_above_150_and_200"]["passed"] is None
    # Only relative strength (independent of price history) can be evaluated here.
    assert result["evaluated_count"] < 8
    assert result["meets_six_of_eight_gate"] is False


def test_missing_required_columns_raises():
    bars = pd.DataFrame({"date": pd.date_range("2023-01-02", periods=5), "close": [1, 2, 3, 4, 5]})

    with pytest.raises(ValueError):
        calculate_trend_template(bars, "daily", config=CONFIG)


def test_strong_uptrend_passes_every_criterion():
    """A long, steady compounding uptrend should stack every moving average
    upward, keep the 200-bar MA rising, and sit at (not below) the 52-week
    high while comfortably above the 52-week low -- an unambiguous pass on
    all eight criteria once a qualifying RS rating is supplied.
    """
    prices = [100 * (1.003**i) for i in range(400)]
    bars = _daily_bars(prices)

    result = calculate_trend_template(bars, "daily", relative_strength_rating=85, config=CONFIG)

    assert result["passed_count"] == 8
    assert result["evaluated_count"] == 8
    assert result["all_criteria_evaluated"] is True
    assert result["meets_six_of_eight_gate"] is True


def test_relative_strength_rating_below_threshold_fails_only_that_criterion():
    prices = [100 * (1.003**i) for i in range(400)]
    bars = _daily_bars(prices)

    result = calculate_trend_template(bars, "daily", relative_strength_rating=40, config=CONFIG)

    assert result["criteria"]["relative_strength_at_least_70"]["passed"] is False
    # Every other (price/MA-based) criterion should still pass.
    assert result["passed_count"] == 7
    assert result["meets_six_of_eight_gate"] is True
