"""Composite score calculation and eligibility gates.

Combines the analytics modules' outputs (Trend Template, market stage,
RS rating, VCP detection, volume/supply-demand signals) into a single
0-100, config-driven, explainable composite score per design doc §7.
Fundamentals stay unevaluated for now: no Screener.in ingestion exists yet
(see PROJECT-CONTEXT.md), so `fundamentals_score` is always `None` and the
composite is renormalized across whichever components were evaluated
rather than silently scoring the missing component as zero.

`calculate_composite_score()` is a pure function over already-computed
analytics (trend template dicts, a stage row, an RS row, a VCP row, a
volume-signals row) -- it does no data fetching or indicator math itself,
mirroring how `detect_vcp()` stays pure while `run_vcp_detection()` handles
I/O. `run_scoring()` bulk-fetches bars and every other module's latest
persisted rows for the active universe, mirroring `run_vcp_detection()` /
`run_volume_signals()`, and upserts one row per symbol into `scores_daily`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

from config.settings import Settings, get_settings
from sepa_scanner.analytics.trend_template import (
    calculate_symbol_trend_template,
    calculate_trend_template,
)
from sepa_scanner.storage.db import initialize_analytics

CONFIG_PATH = Path(__file__).parents[2] / "config" / "scoring_weights.yaml"
VCP_CONFIG_PATH = Path(__file__).parents[2] / "config" / "vcp.yaml"


def calculate_composite_score(
    *,
    daily_template: Mapping[str, Any] | None,
    weekly_template: Mapping[str, Any] | None,
    stage: Mapping[str, Any] | None,
    relative_strength: Mapping[str, Any] | None,
    vcp: Mapping[str, Any] | None,
    volume: Mapping[str, Any] | None,
    fundamentals: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine already-computed analytics into one explainable 0-100 score.

    Each argument is the output (or a stored row) of another analytics
    module: `daily_template`/`weekly_template` from
    `calculate_trend_template()`, `stage` from `market_stages_daily`,
    `relative_strength` from `rs_ratings_daily`, `vcp` from
    `vcp_results_weekly`, `volume` from `volume_signals_daily`. Any argument
    may be `None` when that module has no result yet for this symbol; the
    corresponding component is then excluded (not zeroed) from the
    composite via renormalization, and its own `..._evaluated` flag is
    `False` so the UI can show "not yet evaluated" rather than a
    misleading low score.
    """
    settings = dict(config) if config is not None else _load_config()
    weights = settings["weights"]
    gates = settings["gates"]
    vcp_thresholds = _load_vcp_thresholds()

    trend_score, trend_evaluated, gate_trend_passed = _score_trend_template(
        daily_template, weekly_template, int(gates["trend_template_min_passes"])
    )
    stage_score, stage_evaluated = _score_stage(stage, settings["stage_scores"])
    rs_score, rs_evaluated, gate_rs_passed = _score_relative_strength(
        relative_strength,
        float(gates["relative_strength_min"]),
        float(settings["relative_strength"]["new_high_bonus"]),
    )
    vcp_score, vcp_evaluated = _score_vcp(vcp, vcp_thresholds, settings["vcp_component_weights"])
    volume_score, volume_evaluated = _score_volume(volume, settings["volume"])
    fundamentals_score, fundamentals_evaluated = _score_fundamentals(fundamentals)

    components = {
        "trend_template": (trend_score, trend_evaluated),
        "stage": (stage_score, stage_evaluated),
        "relative_strength": (rs_score, rs_evaluated),
        "vcp_quality": (vcp_score, vcp_evaluated),
        "volume_supply_demand": (volume_score, volume_evaluated),
        "fundamentals": (fundamentals_score, fundamentals_evaluated),
    }
    composite = _combine(components, weights)
    gate_passed = bool(gate_trend_passed and gate_rs_passed)

    as_of_date = None
    if daily_template is not None:
        as_of_date = daily_template.get("as_of_date")
    if as_of_date is None and weekly_template is not None:
        as_of_date = weekly_template.get("as_of_date")

    return {
        "as_of_date": as_of_date,
        "composite_score": composite,
        "gate_passed": gate_passed,
        "gate_trend_template_passed": gate_trend_passed,
        "gate_relative_strength_passed": gate_rs_passed,
        "trend_template_score": trend_score,
        "trend_template_evaluated": trend_evaluated,
        "stage_score": stage_score,
        "stage_evaluated": stage_evaluated,
        "relative_strength_score": rs_score,
        "relative_strength_evaluated": rs_evaluated,
        "vcp_score": vcp_score,
        "vcp_evaluated": vcp_evaluated,
        "volume_score": volume_score,
        "volume_evaluated": volume_evaluated,
        "fundamentals_score": fundamentals_score,
        "fundamentals_evaluated": fundamentals_evaluated,
        "quality_grade": vcp.get("quality_grade") if vcp is not None else None,
        "stage": int(stage["stage"]) if stage is not None and not _is_missing(stage.get("stage")) else None,
        "relative_strength_rating": (
            int(relative_strength["rating"])
            if relative_strength is not None and not _is_missing(relative_strength.get("rating"))
            else None
        ),
    }


def calculate_symbol_score(symbol: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Load one symbol's stored analytics and compute its composite score.

    Convenience wrapper for on-demand use (e.g. a future `/stock/{symbol}`
    endpoint) that recomputes the Trend Template live from stored bars and
    reads the other components' latest persisted rows, without requiring a
    prior `run_scoring()` batch pass for this symbol.
    """
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        rs_row = _fetchone_as_dict(
            connection,
            "SELECT rating, rs_line_new_high FROM rs_ratings_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            [symbol.upper()],
        )
        stage_row = _fetchone_as_dict(
            connection,
            "SELECT stage FROM market_stages_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            [symbol.upper()],
        )
        vcp_row = _fetchone_as_dict(
            connection,
            """SELECT quality_grade, contraction_count, depth_decay_passed, volume_decay_passed,
                      base_length_passed, daily_confirmation_available, near_pivot,
                      tight_closes_confirmed, breakout_confirmed
               FROM vcp_results_weekly WHERE symbol = ? ORDER BY date DESC LIMIT 1""",
            [symbol.upper()],
        )
        volume_row = _fetchone_as_dict(
            connection,
            """SELECT up_down_volume_ratio, accumulation_distribution_rising, volume_spike_near_pivot
               FROM volume_signals_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1""",
            [symbol.upper()],
        )
    finally:
        connection.close()

    rating = float(rs_row["rating"]) if rs_row and not _is_missing(rs_row.get("rating")) else None
    daily_template = calculate_symbol_trend_template(
        symbol, "daily", relative_strength_rating=rating, settings=runtime_settings
    )
    weekly_template = calculate_symbol_trend_template(
        symbol, "weekly", relative_strength_rating=rating, settings=runtime_settings
    )

    result = calculate_composite_score(
        daily_template=daily_template,
        weekly_template=weekly_template,
        stage=stage_row,
        relative_strength=rs_row,
        vcp=vcp_row,
        volume=volume_row,
    )
    result["symbol"] = symbol.upper()
    return result


def run_scoring(settings: Settings | None = None) -> dict[str, int | str]:
    """Calculate and persist the latest composite score for every active symbol.

    Mirrors `run_vcp_detection()` / `run_volume_signals()`: bulk-fetch
    adjusted-price bars plus every other module's latest persisted rows for
    the active universe, score each symbol with `calculate_composite_score()`,
    then upsert one row per symbol into `scores_daily`. Symbols without
    enough daily history to produce an `as_of_date` are skipped rather than
    persisted with a meaningless score.
    """
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        daily_bars = connection.execute(
            """
            SELECT daily.symbol, daily.date,
              coalesce(nullif(daily.adj_close, 0), daily.close) AS close,
              daily.high * coalesce(nullif(daily.adj_close, 0) / nullif(daily.close, 0), 1) AS high,
              daily.low * coalesce(nullif(daily.adj_close, 0) / nullif(daily.close, 0), 1) AS low
            FROM ohlcv_daily daily JOIN universe USING (symbol)
            WHERE universe.is_active = TRUE ORDER BY daily.symbol, daily.date
            """
        ).fetchdf()
        weekly_bars = connection.execute(
            """
            SELECT weekly.symbol, weekly.week_end_date AS date,
              coalesce(nullif(weekly.adj_close, 0), weekly.close) AS close,
              weekly.high * coalesce(nullif(weekly.adj_close, 0) / nullif(weekly.close, 0), 1) AS high,
              weekly.low * coalesce(nullif(weekly.adj_close, 0) / nullif(weekly.close, 0), 1) AS low
            FROM ohlcv_weekly weekly JOIN universe USING (symbol)
            WHERE universe.is_active = TRUE ORDER BY weekly.symbol, weekly.week_end_date
            """
        ).fetchdf()
        rs_latest = connection.execute(
            """SELECT symbol, rating, rs_line_new_high FROM rs_ratings_daily
               WHERE date = (SELECT max(date) FROM rs_ratings_daily)"""
        ).fetchdf()
        stage_latest = connection.execute(
            """SELECT symbol, stage FROM market_stages_daily
               WHERE date = (SELECT max(date) FROM market_stages_daily)"""
        ).fetchdf()
        vcp_latest = connection.execute(
            """SELECT symbol, quality_grade, contraction_count, depth_decay_passed,
                      volume_decay_passed, base_length_passed, daily_confirmation_available,
                      near_pivot, tight_closes_confirmed, breakout_confirmed
               FROM vcp_results_weekly WHERE date = (SELECT max(date) FROM vcp_results_weekly)"""
        ).fetchdf()
        volume_latest = connection.execute(
            """SELECT symbol, up_down_volume_ratio, accumulation_distribution_rising,
                      volume_spike_near_pivot
               FROM volume_signals_daily WHERE date = (SELECT max(date) FROM volume_signals_daily)"""
        ).fetchdf()

        rs_by_symbol = rs_latest.set_index("symbol").to_dict("index")
        stage_by_symbol = stage_latest.set_index("symbol").to_dict("index")
        vcp_by_symbol = vcp_latest.set_index("symbol").to_dict("index")
        volume_by_symbol = volume_latest.set_index("symbol").to_dict("index")
        weekly_by_symbol = {symbol: group for symbol, group in weekly_bars.groupby("symbol", sort=False)}

        records: list[dict[str, Any]] = []
        for symbol, symbol_daily in daily_bars.groupby("symbol", sort=False):
            rs_row = rs_by_symbol.get(symbol)
            rating = float(rs_row["rating"]) if rs_row and not _is_missing(rs_row.get("rating")) else None
            daily_template = calculate_trend_template(symbol_daily, "daily", relative_strength_rating=rating)
            symbol_weekly = weekly_by_symbol.get(symbol)
            weekly_template = (
                calculate_trend_template(symbol_weekly, "weekly", relative_strength_rating=rating)
                if symbol_weekly is not None and not symbol_weekly.empty
                else None
            )
            result = calculate_composite_score(
                daily_template=daily_template,
                weekly_template=weekly_template,
                stage=stage_by_symbol.get(symbol),
                relative_strength=rs_row,
                vcp=vcp_by_symbol.get(symbol),
                volume=volume_by_symbol.get(symbol),
            )
            if result["as_of_date"] is None:
                continue
            record = {"symbol": symbol, "date": result["as_of_date"]}
            record.update({key: value for key, value in result.items() if key != "as_of_date"})
            records.append(record)

        if records:
            staged = pd.DataFrame(records)
            connection.register("staged_scores", staged)
            connection.execute(
                """INSERT INTO scores_daily AS target SELECT * FROM staged_scores
                   ON CONFLICT (symbol, date) DO UPDATE SET
                     composite_score = excluded.composite_score,
                     gate_passed = excluded.gate_passed,
                     gate_trend_template_passed = excluded.gate_trend_template_passed,
                     gate_relative_strength_passed = excluded.gate_relative_strength_passed,
                     trend_template_score = excluded.trend_template_score,
                     trend_template_evaluated = excluded.trend_template_evaluated,
                     stage_score = excluded.stage_score,
                     stage_evaluated = excluded.stage_evaluated,
                     relative_strength_score = excluded.relative_strength_score,
                     relative_strength_evaluated = excluded.relative_strength_evaluated,
                     vcp_score = excluded.vcp_score,
                     vcp_evaluated = excluded.vcp_evaluated,
                     volume_score = excluded.volume_score,
                     volume_evaluated = excluded.volume_evaluated,
                     fundamentals_score = excluded.fundamentals_score,
                     fundamentals_evaluated = excluded.fundamentals_evaluated,
                     quality_grade = excluded.quality_grade,
                     stage = excluded.stage,
                     relative_strength_rating = excluded.relative_strength_rating"""
            )
            connection.unregister("staged_scores")
        return {
            "symbols_scored": len(records),
            "symbols_gate_passed": sum(1 for record in records if record["gate_passed"]),
            "as_of_date": str(daily_bars["date"].max()) if not daily_bars.empty else "",
        }
    finally:
        connection.close()


def latest_scores(settings: Settings | None = None) -> pd.DataFrame:
    """Return the latest persisted composite score for each active symbol,
    ranked gate-passing-first then by score -- the read path for
    `/scores/today`."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        return connection.execute(
            """SELECT scores.* FROM scores_daily scores
               JOIN universe USING (symbol) WHERE universe.is_active = TRUE
                 AND date = (SELECT max(date) FROM scores_daily)
               ORDER BY gate_passed DESC, composite_score DESC, symbol"""
        ).fetchdf()
    finally:
        connection.close()


def _score_trend_template(
    daily_template: Mapping[str, Any] | None,
    weekly_template: Mapping[str, Any] | None,
    min_passes: int,
) -> tuple[float | None, bool, bool]:
    """Average whichever of the daily/weekly pass rates are available.

    The gate (design doc §7.2) checks only the daily template, matching how
    Minervini's own 8-criteria filter is normally applied, and requires all
    8 criteria to have been evaluated (not just enough of them to pass) so a
    symbol with too little history can't gate-pass on a technicality.
    """
    rates = [
        template["pass_rate"]
        for template in (daily_template, weekly_template)
        if template is not None and template.get("pass_rate") is not None
    ]
    evaluated = bool(rates)
    score = (sum(rates) / len(rates)) * 100 if evaluated else None
    gate_passed = bool(
        daily_template is not None
        and daily_template.get("evaluated_count") == daily_template.get("criteria_count")
        and (daily_template.get("passed_count") or 0) >= min_passes
    )
    return score, evaluated, gate_passed


def _score_stage(stage_row: Mapping[str, Any] | None, stage_scores: Mapping[int, Any]) -> tuple[float | None, bool]:
    if stage_row is None or _is_missing(stage_row.get("stage")):
        return None, False
    stage = int(stage_row["stage"])
    return float(stage_scores.get(stage, 0)), True


def _score_relative_strength(
    rs_row: Mapping[str, Any] | None, min_rating: float, new_high_bonus: float
) -> tuple[float | None, bool, bool]:
    if rs_row is None or _is_missing(rs_row.get("rating")):
        return None, False, False
    rating = float(rs_row["rating"])
    score = min(rating + (new_high_bonus if _truthy(rs_row.get("rs_line_new_high")) else 0), 100.0)
    return score, True, rating >= min_rating


def _score_vcp(
    vcp_row: Mapping[str, Any] | None,
    vcp_thresholds: Mapping[str, int],
    component_weights: Mapping[str, float],
) -> tuple[float | None, bool]:
    """Score the persisted VCP structure and daily-confirmation sub-factors.

    `depth_decay_passed`/`volume_decay_passed`/`base_length_passed` are read
    straight from `vcp_results_weekly`; the contraction-count check is
    derived here against `vcp.yaml`'s bounds since that boolean isn't itself
    persisted. Absence of daily confirmation data is treated as neutral
    (50), not a penalty -- a weekly VCP structure can be real before a
    symbol nears its pivot.
    """
    if vcp_row is None:
        return None, False
    contraction_count = vcp_row.get("contraction_count")
    count_ok = not _is_missing(contraction_count) and (
        vcp_thresholds["min_contractions"] <= contraction_count <= vcp_thresholds["max_contractions"]
    )
    structure_flags = [
        _truthy(vcp_row.get("depth_decay_passed")),
        _truthy(vcp_row.get("volume_decay_passed")),
        _truthy(vcp_row.get("base_length_passed")),
        count_ok,
    ]
    structure_score = (sum(structure_flags) / len(structure_flags)) * 100

    if _truthy(vcp_row.get("daily_confirmation_available")):
        confirmation_flags = [
            _truthy(vcp_row.get("near_pivot")),
            _truthy(vcp_row.get("tight_closes_confirmed")),
            _truthy(vcp_row.get("breakout_confirmed")),
        ]
        confirmation_score = (sum(confirmation_flags) / len(confirmation_flags)) * 100
    else:
        confirmation_score = 50.0

    score = (
        structure_score * float(component_weights["structure"])
        + confirmation_score * float(component_weights["daily_confirmation"])
    )
    return score, True


def _score_volume(volume_row: Mapping[str, Any] | None, volume_config: Mapping[str, Any]) -> tuple[float | None, bool]:
    if volume_row is None:
        return None, False
    ratio = volume_row.get("up_down_volume_ratio")
    floor = float(volume_config["ratio_floor"])
    ceiling = float(volume_config["ratio_ceiling"])
    if _is_missing(ratio):
        ratio_score = 50.0  # not enough history for a ratio yet -- neutral, not a penalty
    else:
        clipped = min(max(float(ratio), floor), ceiling)
        ratio_score = (clipped - floor) / (ceiling - floor) * 100 if ceiling > floor else 50.0
    bonus = 0.0
    if _truthy(volume_row.get("accumulation_distribution_rising")):
        bonus += float(volume_config["accumulation_bonus"])
    if _truthy(volume_row.get("volume_spike_near_pivot")):
        bonus += float(volume_config["spike_bonus"])
    return min(ratio_score + bonus, 100.0), True


def _score_fundamentals(fundamentals: Mapping[str, Any] | None) -> tuple[None, bool]:
    """Always unevaluated for now.

    No Screener.in ingestion exists yet (see "Required next steps" in
    PROJECT-CONTEXT.md), so `fundamentals_quarterly` is empty for every
    symbol. Kept as an explicit parameter, rather than dropped, so wiring in
    real fundamentals later is a one-function change, not a signature
    change through `calculate_composite_score()` and its callers.
    """
    del fundamentals  # unused until fundamentals ingestion exists
    return None, False


def _combine(components: Mapping[str, tuple[float | None, bool]], weights: Mapping[str, float]) -> float:
    """Weighted average over evaluated components only, renormalized so a
    missing component (most commonly fundamentals) doesn't silently pull
    the composite down as if it had scored zero."""
    weighted_sum = 0.0
    total_weight = 0.0
    for name, (score, evaluated) in components.items():
        if not evaluated or score is None:
            continue
        weight = float(weights[name])
        weighted_sum += score * weight
        total_weight += weight
    return round(weighted_sum / total_weight, 2) if total_weight > 0 else 0.0


def _is_missing(value: Any) -> bool:
    """True for SQL NULL, which pandas may surface as either None or NaN."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _truthy(value: Any) -> bool:
    """Read a nullable boolean column, treating NULL as False rather than
    letting NaN's Python truthiness silently count as True."""
    return bool(value) if not _is_missing(value) else False


def _fetchone_as_dict(connection: Any, query: str, params: list[Any]) -> dict[str, Any] | None:
    """Run a single-row query and return it as a dict keyed by column name,
    or None if there was no matching row."""
    cursor = connection.execute(query, params)
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [description[0] for description in cursor.description]
    return dict(zip(columns, row))


def _load_config() -> dict[str, Any]:
    """Load scoring weights, gates, and the scorer's own component-scoring
    settings from `config/scoring_weights.yaml`."""
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _load_vcp_thresholds() -> dict[str, int]:
    """Read the contraction-count bounds the scorer needs from `vcp.yaml`.

    `vcp.yaml` remains the single source of truth for VCP pattern
    thresholds (see the comment in `config/scoring_weights.yaml`); the
    scorer reads it directly rather than keeping a second copy of these
    numbers.
    """
    config = yaml.safe_load(VCP_CONFIG_PATH.read_text(encoding="utf-8"))
    weekly = config["weekly"]
    return {
        "min_contractions": int(weekly["min_contractions"]),
        "max_contractions": int(weekly["max_contractions"]),
    }
