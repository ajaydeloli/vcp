"""Tests for sepa_scanner.analytics.stage_classifier.

The regression test below specifically guards against the moving-average
values being picked up positionally from `moving_averages.items()` (which
depended on insertion order matching short/medium/long) instead of by the
numeric period embedded in each key (e.g. "ma_50", "ma_150", "ma_200").
"""

from __future__ import annotations

from sepa_scanner.analytics.stage_classifier import classify_market_stage

CONFIG = {
    "thresholds": {
        "flat_ma_slope_fraction": 0.0,
        "stage_2_min_trend_template_passes": 0,
        "extended_above_short_ma_fraction": 0.5,
    }
}


def _trend_template(moving_averages: dict[str, float | str]) -> dict:
    return {
        "price": 130.0,
        "moving_averages": moving_averages,
        "criteria": {
            "ma_200_trending_up": {"passed": True, "actual": {"one_month_ago": 95.0}},
            "price_above_150_and_200_ma": {"passed": True},
            "ma_50_above_150_and_200": {"passed": True},
        },
    }


def test_moving_averages_assigned_by_period_not_dict_order():
    """Deliberately insert ma_200 first: a positional [0:3] unpack would wrongly
    treat 200 as the "short" period and misclassify the stage entirely.
    """
    out_of_order = {"ma_200": 100.0, "ma_50": 120.0, "ma_150": 110.0, "period_unit": "sessions"}

    result = classify_market_stage(_trend_template(out_of_order), config=CONFIG)

    assert result["actual"]["ma_short"] == 120.0
    assert result["actual"]["ma_medium"] == 110.0
    assert result["actual"]["ma_long"] == 100.0
    # price(130) > ma_short(120) > ma_medium(110) > ma_long(100), slope positive:
    # a textbook Stage 2 advance.
    assert result["stage"] == 2


def test_moving_averages_assigned_correctly_when_already_in_order():
    in_order = {"ma_50": 120.0, "ma_150": 110.0, "ma_200": 100.0, "period_unit": "sessions"}

    result = classify_market_stage(_trend_template(in_order), config=CONFIG)

    assert result["actual"]["ma_short"] == 120.0
    assert result["actual"]["ma_medium"] == 110.0
    assert result["actual"]["ma_long"] == 100.0
    assert result["stage"] == 2
