"""Tests for sepa_scanner.analytics.volume_signals -- pure calculations."""

from __future__ import annotations

import pandas as pd
import pytest

from sepa_scanner.analytics.volume_signals import calculate_volume_signals


def _bars(closes: list[float], volumes: list[int], *, high_offset=0.0, low_offset=0.0) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=len(closes), freq="D")
    return pd.DataFrame({
        "date": dates,
        "close": closes,
        "high": [c + high_offset for c in closes],
        "low": [c - low_offset for c in closes],
        "volume": volumes,
    })


def test_missing_required_columns_raises():
    bars = pd.DataFrame({"date": pd.date_range("2024-01-02", periods=3), "close": [1, 2, 3]})
    with pytest.raises(ValueError):
        calculate_volume_signals(bars, config={
            "up_down_volume": {"lookback_sessions": 1},
            "accumulation_distribution": {"lookback_sessions": 1},
            "pivot_volume_spike": {"lookback_sessions": 1, "proximity_fraction": 0.05,
                                    "volume_average_lookback": 1, "volume_multiple": 1.4},
        })


def test_up_down_volume_insufficient_history_reports_none():
    bars = _bars([100, 101, 99, 102, 98], [100, 100, 100, 100, 100])  # 5 bars, lookback needs 6
    config = {
        "up_down_volume": {"lookback_sessions": 5},
        "accumulation_distribution": {"lookback_sessions": 5},
        "pivot_volume_spike": {"lookback_sessions": 5, "proximity_fraction": 0.05,
                                "volume_average_lookback": 5, "volume_multiple": 1.4},
    }
    result = calculate_volume_signals(bars, config=config)

    assert result["up_down_volume"]["sufficient_history"] is False
    assert result["up_down_volume"]["ratio"] is None


def test_up_down_volume_ratio_sums_correctly():
    # Diffs from bar 1 on: +1, -2, +3, -4, +5 -> up bars at 1,3,5; down bars at 2,4.
    closes = [100, 101, 99, 102, 98, 103]
    volumes = [1000, 200, 150, 300, 250, 400]
    bars = _bars(closes, volumes)
    config = {
        "up_down_volume": {"lookback_sessions": 5},
        "accumulation_distribution": {"lookback_sessions": 5},
        "pivot_volume_spike": {"lookback_sessions": 5, "proximity_fraction": 0.05,
                                "volume_average_lookback": 5, "volume_multiple": 1.4},
    }
    result = calculate_volume_signals(bars, config=config)
    up_down = result["up_down_volume"]

    assert up_down["sufficient_history"] is True
    assert up_down["up_volume"] == 200 + 300 + 400
    assert up_down["down_volume"] == 150 + 250
    assert up_down["ratio"] == pytest.approx((200 + 300 + 400) / (150 + 250))


def test_accumulation_distribution_rising_trend_and_zero_range_guard():
    # Every bar closes 0.6 of the way up its own high-low range (close is
    # nearer the high than the low), so money-flow is a constant positive
    # 0.6 * volume each bar regardless of the closes' own up/down movement --
    # a clean, deterministic monotonically rising A/D line.
    closes = [100, 99, 101, 98, 105]  # deliberately non-monotonic prices
    bars = _bars(closes, [1000] * 5, high_offset=0.2, low_offset=0.8)
    config = {
        "up_down_volume": {"lookback_sessions": 3},
        "accumulation_distribution": {"lookback_sessions": 3},
        "pivot_volume_spike": {"lookback_sessions": 3, "proximity_fraction": 0.05,
                                "volume_average_lookback": 3, "volume_multiple": 1.4},
    }
    result = calculate_volume_signals(bars, config=config)
    accumulation = result["accumulation_distribution"]

    assert accumulation["sufficient_history"] is True
    assert accumulation["trend_rising"] is True
    assert accumulation["current_value"] == pytest.approx(0.6 * 1000 * 5)


def test_accumulation_distribution_handles_zero_high_low_range():
    """A bar where high == low (no intraday range) must not raise or produce NaN."""
    bars = _bars([100, 100], [500, 500], high_offset=0.0, low_offset=0.0)
    config = {
        "up_down_volume": {"lookback_sessions": 1},
        "accumulation_distribution": {"lookback_sessions": 1},
        "pivot_volume_spike": {"lookback_sessions": 1, "proximity_fraction": 0.05,
                                "volume_average_lookback": 1, "volume_multiple": 1.4},
    }
    result = calculate_volume_signals(bars, config=config)

    assert result["accumulation_distribution"]["current_value"] == 0.0


def test_pivot_volume_spike_not_available_without_pivot_price():
    bars = _bars([100, 101, 102], [100, 100, 100])
    config = {
        "up_down_volume": {"lookback_sessions": 2},
        "accumulation_distribution": {"lookback_sessions": 2},
        "pivot_volume_spike": {"lookback_sessions": 2, "proximity_fraction": 0.05,
                                "volume_average_lookback": 2, "volume_multiple": 1.4},
    }
    result = calculate_volume_signals(bars, config=config)

    assert result["pivot_volume_spike"]["available"] is False
    assert result["pivot_volume_spike"]["spike_detected"] is False


def test_pivot_volume_spike_detects_up_day_near_pivot_on_high_volume():
    closes = [100, 101, 102, 100.5, 103, 104]
    volumes = [400, 450, 500, 500, 300, 1000]
    bars = _bars(closes, volumes)
    config = {
        "up_down_volume": {"lookback_sessions": 5},
        "accumulation_distribution": {"lookback_sessions": 5},
        "pivot_volume_spike": {"lookback_sessions": 5, "proximity_fraction": 0.02,
                                "volume_average_lookback": 3, "volume_multiple": 1.5},
    }
    result = calculate_volume_signals(bars, pivot_price=104.0, config=config)
    spike = result["pivot_volume_spike"]

    assert spike["available"] is True
    assert spike["spike_detected"] is True
    assert spike["spike_volume"] == 1000
    assert spike["spike_average_volume"] == pytest.approx((500 + 500 + 300) / 3)


def test_pivot_volume_spike_absent_when_volume_does_not_clear_multiple():
    closes = [100, 101, 102, 100.5, 103, 104]
    volumes = [400, 450, 500, 500, 300, 600]  # same shape, but final volume too low
    bars = _bars(closes, volumes)
    config = {
        "up_down_volume": {"lookback_sessions": 5},
        "accumulation_distribution": {"lookback_sessions": 5},
        "pivot_volume_spike": {"lookback_sessions": 5, "proximity_fraction": 0.02,
                                "volume_average_lookback": 3, "volume_multiple": 1.5},
    }
    result = calculate_volume_signals(bars, pivot_price=104.0, config=config)

    assert result["pivot_volume_spike"]["spike_detected"] is False
