"""Tests for sepa_scanner.analytics.vcp_detector.

Uses an explicit config dict (rather than config/vcp.yaml) so these tests stay
correct even if the on-disk defaults are retuned later.
"""

from __future__ import annotations

import pandas as pd

from sepa_scanner.analytics.vcp_detector import detect_vcp

CONFIG = {
    "weekly": {
        "lookback_weeks": 65,
        "swing_window_weeks": 2,
        "min_contractions": 2,
        "max_contractions": 4,
        "contraction_tolerance": 0.80,
        "volume_decay_min": 0.40,
        "volume_decay_max": 0.60,
        "base_length_min_weeks": 5,
        "base_length_max_weeks": 65,
    },
    "daily_confirmation": {
        "lookback_sessions": 10,
        "tight_close_tolerance": 0.015,
        "minimum_tight_closes": 3,
        "pivot_proximity": 0.05,
        "breakout_volume_lookback": 20,
        "breakout_volume_multiple": 1.40,
    },
}

EMPTY_DAILY = pd.DataFrame(columns=["date", "close", "high", "low", "volume"])


def _textbook_vcp_weekly_bars() -> pd.DataFrame:
    """Three successively shallower, lower-volume pullbacks: a textbook VCP.

    Contraction depths run 20% -> 10% -> 5% (each exactly half the last) and
    leg volumes run 2.0M -> 1.0M -> 0.5M (also exactly half each time), which
    sit inside the configured 0.80 depth-decay ratio and 0.40-0.60 volume-decay
    band. Each swing high/low is a strict local extreme within the +/-2-week
    swing-detection window, verified by hand against `_weekly_swings`.
    """
    prices = [
        100, 105, 110, 115, 120, 125, 130,  # rally into Peak 1 (index 6)
        122, 113, 104,  # pullback into Trough 1 (index 9) -- depth 20%
        111, 118, 124,  # rally into Peak 2 (index 12)
        118, 114, 111.6,  # pullback into Trough 2 (index 15) -- depth 10%
        115, 118, 120,  # rally into Peak 3 (index 18)
        118, 116, 114,  # pullback into Trough 3 (index 21) -- depth 5%
        114.5, 115.5,  # tight two-week base after the low
    ]
    volumes = [1_000_000] * len(prices)
    for index in (6, 7, 8, 9):
        volumes[index] = 2_000_000
    for index in (18, 19, 20, 21):
        volumes[index] = 500_000
    # indices 12-15 (leg 2) keep the default 1,000,000 -- exactly half of leg 1.

    dates = pd.date_range("2024-01-07", periods=len(prices), freq="7D")
    return pd.DataFrame(
        {"date": dates, "close": prices, "high": prices, "low": prices, "volume": volumes}
    )


def test_detects_textbook_contracting_vcp():
    result = detect_vcp(_textbook_vcp_weekly_bars(), EMPTY_DAILY, config=CONFIG)

    assert result["is_vcp"] is True
    assert result["weekly"]["contraction_count"] == 3
    assert result["weekly"]["depth_decay_passed"] is True
    assert result["weekly"]["volume_decay_passed"] is True
    assert result["weekly"]["base_length_passed"] is True
    assert result["pivot"]["price"] == 120
    # No daily bars supplied, so quality grade falls back to the weekly-only tier.
    assert result["quality_grade"] == "B"


def test_insufficient_weekly_history_is_reported_not_silently_failed():
    short_bars = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-07", periods=4, freq="7D"),
            "close": [100, 101, 102, 103],
            "high": [100, 101, 102, 103],
            "low": [100, 101, 102, 103],
            "volume": [1_000_000] * 4,
        }
    )

    result = detect_vcp(short_bars, EMPTY_DAILY, config=CONFIG)

    assert result["is_vcp"] is False
    assert result["reason"] == "Insufficient weekly history"


def test_expanding_pullbacks_are_rejected():
    """Same swing shape as the textbook fixture, but each pullback is deeper
    than the last (5% -> 10% -> 20%) -- the opposite of a genuine VCP.

    Because every step-to-step depth ratio here exceeds the contraction
    tolerance, `_latest_contracting_sequence` cannot chain any two adjacent
    contractions together and falls back to the single most recent one, so
    this is rejected on contraction *count* (only 1, below the configured
    minimum of 2) rather than ever reaching the depth-decay check.
    """
    prices = [
        100, 105, 110, 115, 120, 125, 130,  # Peak 1 (index 6)
        128, 126, 123.5,  # Trough 1 (index 9) -- depth 5%
        126, 130, 135,  # Peak 2 (index 12)
        130, 125, 121.5,  # Trough 2 (index 15) -- depth 10%
        124, 128, 132,  # Peak 3 (index 18)
        122, 112, 105.6,  # Trough 3 (index 21) -- depth 20%
        107, 109,
    ]
    volumes = [1_000_000] * len(prices)
    dates = pd.date_range("2024-01-07", periods=len(prices), freq="7D")
    weekly = pd.DataFrame(
        {"date": dates, "close": prices, "high": prices, "low": prices, "volume": volumes}
    )

    result = detect_vcp(weekly, EMPTY_DAILY, config=CONFIG)

    assert result["is_vcp"] is False
    assert result["weekly"]["contraction_count"] < CONFIG["weekly"]["min_contractions"]
