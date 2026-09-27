"""Minervini trend-template calculations for daily and weekly bars."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import pandas as pd
import yaml

from config.settings import Settings, get_settings
from sepa_scanner.storage.db import initialize_analytics

Timeframe = Literal["daily", "weekly"]
CONFIG_PATH = Path(__file__).parents[2] / "config" / "trend_template.yaml"


def calculate_trend_template(
    bars: pd.DataFrame | Sequence[dict[str, Any]],
    timeframe: Timeframe,
    *,
    relative_strength_rating: float | None = None,
    minimum_gain_from_52_week_low: float | None = None,
    maximum_decline_from_52_week_high: float | None = None,
    minimum_relative_strength_rating: float | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return all eight template criteria with measured values and pass states.

    Input bars must contain ``date``, ``close``, ``high``, and ``low`` columns.
    ``close`` should be adjusted for corporate actions when adjusted data is
    available; ``high`` and ``low`` should use the same price basis.
    Insufficient history or an unavailable RS rating yields ``passed: None``.
    """
    if timeframe not in ("daily", "weekly"):
        raise ValueError("timeframe must be 'daily' or 'weekly'")
    settings = dict(config) if config is not None else _load_config()
    criteria_settings = settings["criteria"]
    if minimum_gain_from_52_week_low is None:
        minimum_gain_from_52_week_low = float(criteria_settings["minimum_gain_from_52_week_low"])
    if maximum_decline_from_52_week_high is None:
        maximum_decline_from_52_week_high = float(criteria_settings["maximum_decline_from_52_week_high"])
    if minimum_relative_strength_rating is None:
        minimum_relative_strength_rating = float(criteria_settings["minimum_relative_strength_rating"])
    if minimum_gain_from_52_week_low < 0:
        raise ValueError("minimum_gain_from_52_week_low cannot be negative")
    if not 0 <= maximum_decline_from_52_week_high < 1:
        raise ValueError("maximum_decline_from_52_week_high must be in [0, 1)")

    frame = pd.DataFrame(bars).copy()
    required = {"date", "close", "high", "low"}
    if not required.issubset(frame.columns):
        raise ValueError(f"bars must contain columns: {', '.join(sorted(required))}")
    frame = frame.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    for column in ("close", "high", "low"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["close", "high", "low"])
    frame = frame[(frame["close"] > 0) & (frame["high"] > 0) & (frame["low"] > 0)]

    periods = settings["periods"][timeframe]
    short, medium, long = periods["short"], periods["medium"], periods["long"]
    criteria: dict[str, dict[str, Any]] = {}

    def add(key: str, description: str, passed: bool | None, actual: dict[str, Any], threshold: Any) -> None:
        criteria[key] = {
            "description": description,
            "passed": passed,
            "actual": actual,
            "threshold": threshold,
        }

    if frame.empty:
        latest_price = None
        latest_date = None
        ma_short = ma_medium = ma_long = None
    else:
        latest = frame.iloc[-1]
        latest_price = float(latest["close"])
        latest_date_value = latest["date"]
        latest_date = latest_date_value.isoformat() if hasattr(latest_date_value, "isoformat") else str(latest_date_value)
        ma_short = _last_mean(frame["close"], short)
        ma_medium = _last_mean(frame["close"], medium)
        ma_long = _last_mean(frame["close"], long)

    enough_ma_history = len(frame) >= long
    add(
        "price_above_150_and_200_ma",
        f"Price above {medium}- and {long}-bar moving averages",
        (latest_price > ma_medium and latest_price > ma_long) if enough_ma_history else None,
        {"price": latest_price, f"ma_{medium}": ma_medium, f"ma_{long}": ma_long},
        {"price_above": [f"ma_{medium}", f"ma_{long}"]},
    )
    add(
        "ma_150_above_200",
        f"{medium}-bar moving average above {long}-bar moving average",
        (ma_medium > ma_long) if enough_ma_history else None,
        {f"ma_{medium}": ma_medium, f"ma_{long}": ma_long},
        {f"ma_{medium}_greater_than": f"ma_{long}"},
    )

    ma_long_prior = _mean_at_offset(frame["close"], long, periods["month"])
    enough_slope_history = len(frame) >= long + periods["month"]
    add(
        "ma_200_trending_up",
        f"{long}-bar moving average rising over approximately one month",
        (ma_long > ma_long_prior) if enough_slope_history and ma_long is not None and ma_long_prior is not None else None,
        {"current": ma_long, "one_month_ago": ma_long_prior, "lookback_bars": periods["month"]},
        {"direction": "rising"},
    )

    enough_ma_history = len(frame) >= long
    add(
        "ma_50_above_150_and_200",
        f"{short}-bar moving average above {medium}- and {long}-bar moving averages",
        (ma_short > ma_medium and ma_short > ma_long) if enough_ma_history else None,
        {f"ma_{short}": ma_short, f"ma_{medium}": ma_medium, f"ma_{long}": ma_long},
        {f"ma_{short}_greater_than": [f"ma_{medium}", f"ma_{long}"]},
    )
    add(
        "price_above_50_ma",
        f"Price above {short}-bar moving average",
        (latest_price > ma_short) if enough_ma_history else None,
        {"price": latest_price, f"ma_{short}": ma_short},
        {"price_above": f"ma_{short}"},
    )

    year_window = periods["year"]
    enough_year_history = len(frame) >= year_window
    if enough_year_history:
        year_frame = frame.tail(year_window)
        low_52 = float(year_frame["low"].min())
        high_52 = float(year_frame["high"].max())
    else:
        low_52 = high_52 = None
    min_price = low_52 * (1 + minimum_gain_from_52_week_low) if low_52 is not None else None
    max_drop_floor = high_52 * (1 - maximum_decline_from_52_week_high) if high_52 is not None else None
    add(
        "price_at_least_25pct_above_52_week_low",
        "Price sufficiently above the 52-week low",
        (latest_price >= min_price) if enough_year_history else None,
        {"price": latest_price, "low_52_week": low_52, "percent_above_low": _relative_change(low_52, latest_price)},
        {"minimum_gain_fraction": minimum_gain_from_52_week_low, "minimum_price": min_price},
    )
    add(
        "price_within_25pct_of_52_week_high",
        "Price within the configured distance of the 52-week high",
        (latest_price >= max_drop_floor) if enough_year_history else None,
        {"price": latest_price, "high_52_week": high_52, "percent_below_high": _percent_below(high_52, latest_price)},
        {"maximum_decline_fraction": maximum_decline_from_52_week_high, "minimum_price": max_drop_floor},
    )
    add(
        "relative_strength_at_least_70",
        "Relative strength rating meets the configured minimum",
        (relative_strength_rating >= minimum_relative_strength_rating)
        if relative_strength_rating is not None
        else None,
        {"rating": relative_strength_rating},
        {"minimum_rating": minimum_relative_strength_rating},
    )

    passed_count = sum(item["passed"] is True for item in criteria.values())
    evaluated_count = sum(item["passed"] is not None for item in criteria.values())
    return {
        "timeframe": timeframe,
        "as_of_date": latest_date,
        "bar_count": len(frame),
        "price": latest_price,
        "moving_averages": {
            f"ma_{short}": ma_short,
            f"ma_{medium}": ma_medium,
            f"ma_{long}": ma_long,
            "period_unit": "sessions" if timeframe == "daily" else "weeks",
        },
        "criteria": criteria,
        "passed_count": passed_count,
        "evaluated_count": evaluated_count,
        "criteria_count": len(criteria),
        "pass_rate": passed_count / evaluated_count if evaluated_count else None,
        "meets_six_of_eight_gate": passed_count >= 6 and evaluated_count == 8,
        "minimum_history_available": all(item["passed"] is not None for key, item in criteria.items() if key != "relative_strength_at_least_70"),
        "all_criteria_evaluated": evaluated_count == 8,
    }


def calculate_symbol_trend_template(
    symbol: str,
    timeframe: Timeframe = "daily",
    *,
    relative_strength_rating: float | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Load one symbol's stored bars and calculate its Trend Template."""
    if timeframe not in ("daily", "weekly"):
        raise ValueError("timeframe must be 'daily' or 'weekly'")
    runtime_settings = settings or get_settings()
    table, date_column = ("ohlcv_daily", "date") if timeframe == "daily" else ("ohlcv_weekly", "week_end_date")
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        rows = connection.execute(
            f"""
            SELECT {date_column} AS date,
              coalesce(nullif(adj_close, 0), close) AS close,
              high * coalesce(nullif(adj_close, 0) / nullif(close, 0), 1) AS high,
              low * coalesce(nullif(adj_close, 0) / nullif(close, 0), 1) AS low
            FROM {table}
            WHERE symbol = ?
            ORDER BY {date_column}
            """,
            [symbol.upper()],
        ).fetchdf()
        if relative_strength_rating is None and not rows.empty:
            rating_row = connection.execute(
                "SELECT rating FROM rs_ratings_daily WHERE symbol = ? AND date = ?",
                [symbol.upper(), rows["date"].iloc[-1]],
            ).fetchone()
            if rating_row is not None:
                relative_strength_rating = float(rating_row[0])
    finally:
        connection.close()

    result = calculate_trend_template(
        rows,
        timeframe,
        relative_strength_rating=relative_strength_rating,
    )
    result["symbol"] = symbol.upper()
    result["relative_strength_rating"] = relative_strength_rating
    return result


def _last_mean(values: pd.Series, period: int) -> float | None:
    """Return a full-window moving average at the latest bar, if available."""
    if len(values) < period:
        return None
    return float(values.rolling(window=period, min_periods=period).mean().iloc[-1])


def _mean_at_offset(values: pd.Series, period: int, offset: int) -> float | None:
    """Return a full-window moving average `offset` bars before the latest bar."""
    position = len(values) - 1 - offset
    if position < period - 1:
        return None
    return float(values.iloc[: position + 1].rolling(window=period, min_periods=period).mean().iloc[-1])


def _relative_change(reference: float | None, value: float | None) -> float | None:
    """Calculate (value - reference) / reference when reference is meaningful."""
    if reference is None or value is None or reference == 0:
        return None
    return (value - reference) / reference


def _percent_below(reference: float | None, value: float | None) -> float | None:
    """Calculate the fraction a value is below a positive reference value."""
    if reference is None or value is None or reference == 0:
        return None
    return (reference - value) / reference


def calculate_symbol_trend_templates(
    symbol: str,
    *,
    relative_strength_rating: float | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Calculate daily and weekly templates for one symbol."""
    return {
        timeframe: calculate_symbol_trend_template(
            symbol,
            timeframe,
            relative_strength_rating=relative_strength_rating,
            settings=settings,
        )
        for timeframe in ("daily", "weekly")
    }


def _load_config() -> dict[str, Any]:
    """Read period lengths and screening thresholds from the project YAML config."""
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    for timeframe in ("daily", "weekly"):
        periods = config["periods"][timeframe]
        if any(not isinstance(value, int) or value < 1 for value in periods.values()):
            raise ValueError(f"Invalid positive-integer period in {timeframe} Trend Template config")
    return config
