"""Unit tests for the pure composite-scoring function.

`calculate_composite_score()` consumes already-computed analytics (trend
template dicts, a stage row, an RS row, a VCP row, a volume-signals row),
so these are hand-built fixtures matching each module's documented output
shape, not real bars -- the real-bars path is exercised end-to-end in
test_scorer_persistence.py.
"""

from __future__ import annotations

from sepa_scanner.scoring.scorer import _load_config, calculate_composite_score

TEST_CONFIG = {
    "weights": {
        "trend_template": 0.25,
        "stage": 0.10,
        "relative_strength": 0.20,
        "vcp_quality": 0.30,
        "volume_supply_demand": 0.05,
        # Kept at 0 to match the real config: fundamentals is a qualifier,
        # not a composite input (see test_fundamentals_weight_is_zero_*
        # below for the decision this locks in).
        "fundamentals": 0.0,
    },
    "gates": {"trend_template_min_passes": 6, "relative_strength_min": 70},
    "stage_scores": {1: 50, 2: 100, 3: 25, 4: 0},
    "relative_strength": {"new_high_bonus": 5},
    "vcp_component_weights": {"structure": 0.70, "daily_confirmation": 0.30},
    "volume": {"ratio_floor": 0.5, "ratio_ceiling": 2.0, "accumulation_bonus": 15, "spike_bonus": 15},
}


def _passing_daily_template(*, passed_count: int = 8) -> dict:
    return {
        "as_of_date": "2024-06-14",
        "passed_count": passed_count,
        "evaluated_count": 8,
        "criteria_count": 8,
        "pass_rate": passed_count / 8,
    }


def _passing_weekly_template() -> dict:
    return {"as_of_date": "2024-06-14", "passed_count": 7, "evaluated_count": 8, "criteria_count": 8, "pass_rate": 7 / 8}


def _stage_row(stage: int = 2) -> dict:
    return {"stage": stage}


def _rs_row(rating: float = 85, new_high: bool = True) -> dict:
    return {"rating": rating, "rs_line_new_high": new_high}


def _vcp_row(**overrides) -> dict:
    row = {
        "quality_grade": "A+",
        "contraction_count": 3,
        "depth_decay_passed": True,
        "volume_decay_passed": True,
        "base_length_passed": True,
        "daily_confirmation_available": True,
        "near_pivot": True,
        "tight_closes_confirmed": True,
        "breakout_confirmed": False,
    }
    row.update(overrides)
    return row


def _volume_row(**overrides) -> dict:
    row = {"up_down_volume_ratio": 1.8, "accumulation_distribution_rising": True, "volume_spike_near_pivot": True}
    row.update(overrides)
    return row


def test_full_gate_passing_symbol_scores_high_and_passes_the_gate():
    result = calculate_composite_score(
        daily_template=_passing_daily_template(),
        weekly_template=_passing_weekly_template(),
        stage=_stage_row(),
        relative_strength=_rs_row(),
        vcp=_vcp_row(),
        volume=_volume_row(),
        config=TEST_CONFIG,
    )

    assert result["gate_passed"] is True
    assert result["gate_trend_template_passed"] is True
    assert result["gate_relative_strength_passed"] is True
    assert result["composite_score"] > 80
    assert result["quality_grade"] == "A+"
    assert result["stage"] == 2
    assert result["relative_strength_rating"] == 85
    assert result["as_of_date"] == "2024-06-14"
    assert result["fundamentals_evaluated"] is False
    assert result["fundamentals_score"] is None


def test_gate_fails_when_trend_template_has_fewer_than_six_passes():
    result = calculate_composite_score(
        daily_template=_passing_daily_template(passed_count=5),
        weekly_template=_passing_weekly_template(),
        stage=_stage_row(),
        relative_strength=_rs_row(),
        vcp=_vcp_row(),
        volume=_volume_row(),
        config=TEST_CONFIG,
    )

    assert result["gate_trend_template_passed"] is False
    assert result["gate_passed"] is False
    # A soft score is still produced -- the gate hides a symbol from the
    # default view, it doesn't stop the composite from being computed.
    assert result["trend_template_evaluated"] is True


def test_gate_fails_when_rs_rating_below_threshold():
    result = calculate_composite_score(
        daily_template=_passing_daily_template(),
        weekly_template=_passing_weekly_template(),
        stage=_stage_row(),
        relative_strength=_rs_row(rating=65),
        vcp=_vcp_row(),
        volume=_volume_row(),
        config=TEST_CONFIG,
    )

    assert result["gate_relative_strength_passed"] is False
    assert result["gate_passed"] is False


def test_trend_template_uninevaluated_when_neither_timeframe_has_a_pass_rate():
    insufficient = {"as_of_date": None, "passed_count": 0, "evaluated_count": 0, "criteria_count": 8, "pass_rate": None}
    result = calculate_composite_score(
        daily_template=insufficient,
        weekly_template=insufficient,
        stage=_stage_row(),
        relative_strength=_rs_row(),
        vcp=_vcp_row(),
        volume=_volume_row(),
        config=TEST_CONFIG,
    )

    assert result["trend_template_evaluated"] is False
    assert result["trend_template_score"] is None
    assert result["gate_trend_template_passed"] is False
    # as_of_date falls back to None cleanly rather than raising.
    assert result["as_of_date"] is None


def test_missing_components_are_excluded_not_zeroed():
    """Fundamentals is always missing; stage and volume are also missing
    here -- the composite should renormalize across trend template, RS, and
    VCP only, not silently average in zeros for the missing three."""
    only_trend_rs_vcp = calculate_composite_score(
        daily_template=_passing_daily_template(),
        weekly_template=_passing_weekly_template(),
        stage=None,
        relative_strength=_rs_row(),
        vcp=_vcp_row(),
        volume=None,
        config=TEST_CONFIG,
    )
    full = calculate_composite_score(
        daily_template=_passing_daily_template(),
        weekly_template=_passing_weekly_template(),
        stage=_stage_row(),
        relative_strength=_rs_row(),
        vcp=_vcp_row(),
        volume=_volume_row(),
        config=TEST_CONFIG,
    )

    assert only_trend_rs_vcp["stage_evaluated"] is False
    assert only_trend_rs_vcp["stage_score"] is None
    assert only_trend_rs_vcp["volume_evaluated"] is False
    # Both symbols score well since the evaluated components are strong;
    # the missing components shouldn't drag one symbol down relative to
    # the other just because fewer components were available.
    assert only_trend_rs_vcp["composite_score"] > 70
    assert full["composite_score"] > 70


def test_vcp_daily_confirmation_unavailable_is_neutral_not_penalized():
    with_confirmation = calculate_composite_score(
        daily_template=_passing_daily_template(),
        weekly_template=_passing_weekly_template(),
        stage=_stage_row(),
        relative_strength=_rs_row(),
        vcp=_vcp_row(daily_confirmation_available=True, near_pivot=False, tight_closes_confirmed=False, breakout_confirmed=False),
        volume=_volume_row(),
        config=TEST_CONFIG,
    )
    without_confirmation = calculate_composite_score(
        daily_template=_passing_daily_template(),
        weekly_template=_passing_weekly_template(),
        stage=_stage_row(),
        relative_strength=_rs_row(),
        vcp=_vcp_row(daily_confirmation_available=False, near_pivot=None, tight_closes_confirmed=None, breakout_confirmed=None),
        volume=_volume_row(),
        config=TEST_CONFIG,
    )

    # No daily confirmation data (neutral 50) should score at least as well
    # as daily confirmation data that actively failed on every sub-factor.
    assert without_confirmation["vcp_score"] >= with_confirmation["vcp_score"]


def test_volume_missing_ratio_is_neutral_not_zero():
    result = calculate_composite_score(
        daily_template=_passing_daily_template(),
        weekly_template=_passing_weekly_template(),
        stage=_stage_row(),
        relative_strength=_rs_row(),
        vcp=_vcp_row(),
        volume=_volume_row(up_down_volume_ratio=None, accumulation_distribution_rising=None, volume_spike_near_pivot=None),
        config=TEST_CONFIG,
    )

    assert result["volume_evaluated"] is True
    assert result["volume_score"] == 50.0


def test_relative_strength_new_high_bonus_is_capped_at_100():
    result = calculate_composite_score(
        daily_template=_passing_daily_template(),
        weekly_template=_passing_weekly_template(),
        stage=_stage_row(),
        relative_strength=_rs_row(rating=98, new_high=True),
        vcp=_vcp_row(),
        volume=_volume_row(),
        config=TEST_CONFIG,
    )

    assert result["relative_strength_score"] == 100.0


def test_fundamentals_weight_is_zero_in_the_real_config():
    """Locks in a deliberate design decision (see PROJECT-CONTEXT.md):
    fundamentals is a qualifier/filter on the technical setup, not a
    blended composite input. If this ever gets bumped above 0 again, it
    should be a conscious, backtested change -- not a silent regression
    from someone re-adding a default weight.
    """
    assert _load_config()["weights"]["fundamentals"] == 0.0


def test_fundamentals_weight_zero_means_an_evaluated_score_cannot_move_the_composite():
    """Even if a fundamentals engine existed and returned a real, evaluated
    score, a weight of 0 must make it contribute nothing -- proving the
    exclusion is enforced by the weight itself, not by fundamentals simply
    never being evaluated today."""
    shared_kwargs = dict(
        daily_template=_passing_daily_template(),
        weekly_template=_passing_weekly_template(),
        stage=_stage_row(),
        relative_strength=_rs_row(),
        vcp=_vcp_row(),
        volume=_volume_row(),
        config=TEST_CONFIG,
    )

    without_fundamentals = calculate_composite_score(fundamentals=None, **shared_kwargs)
    # calculate_composite_score() doesn't yet read the fundamentals argument
    # (the engine isn't built), so this directly checks the weight's effect
    # by calling _combine() the same way the function does internally, with
    # a hypothetical fundamentals score mixed in.
    from sepa_scanner.scoring.scorer import _combine

    components_without = {
        "trend_template": (without_fundamentals["trend_template_score"], True),
        "stage": (without_fundamentals["stage_score"], True),
        "relative_strength": (without_fundamentals["relative_strength_score"], True),
        "vcp_quality": (without_fundamentals["vcp_score"], True),
        "volume_supply_demand": (without_fundamentals["volume_score"], True),
        "fundamentals": (None, False),
    }
    components_with_low_fundamentals = dict(components_without, fundamentals=(5.0, True))
    components_with_high_fundamentals = dict(components_without, fundamentals=(95.0, True))

    baseline = _combine(components_without, TEST_CONFIG["weights"])
    with_low = _combine(components_with_low_fundamentals, TEST_CONFIG["weights"])
    with_high = _combine(components_with_high_fundamentals, TEST_CONFIG["weights"])

    assert baseline == with_low == with_high
