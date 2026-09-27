"""Explainable Minervini-style market-stage classification."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

from config.settings import Settings, get_settings
from sepa_scanner.analytics.trend_template import calculate_trend_template
from sepa_scanner.storage.db import initialize_analytics

CONFIG_PATH = Path(__file__).parents[2] / "config" / "market_stage.yaml"


def classify_market_stage(
    trend_template: Mapping[str, Any], *, config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Classify a stock into stages 1–4 using moving-average structure."""
    settings = dict(config) if config is not None else _load_config()
    thresholds = settings["thresholds"]
    averages = trend_template["moving_averages"]
    actual = trend_template["criteria"]["ma_200_trending_up"]["actual"]
    price = trend_template["price"]
    ma_periods = sorted(
        (int(key.removeprefix("ma_")), value)
        for key, value in averages.items()
        if key.startswith("ma_") and key.removeprefix("ma_").isdigit()
    )
    ma_short, ma_medium, ma_long = (value for _, value in ma_periods[:3])
    prior_long = actual["one_month_ago"]
    slope = (ma_long - prior_long) / prior_long if ma_long and prior_long else None
    technical_passes = sum(
        item["passed"] is True
        for key, item in trend_template["criteria"].items()
        if key != "relative_strength_at_least_70"
    )
    if price is None or any(value is None for value in (ma_short, ma_medium, ma_long, slope)):
        return {
            "stage": None,
            "label": "Insufficient history",
            "reason": "A complete moving-average structure is required.",
            "actual": {"price": price, "ma_short": ma_short, "ma_medium": ma_medium,
                       "ma_long": ma_long, "ma_long_slope": slope,
                       "trend_template_passes": technical_passes},
        }

    flat = float(thresholds["flat_ma_slope_fraction"])
    bullish = price > ma_short > ma_medium > ma_long
    bearish = price < ma_short < ma_medium < ma_long
    if bullish and slope > flat and technical_passes >= int(thresholds["stage_2_min_trend_template_passes"]):
        stage, label, reason = 2, "Stage 2 — Advancing", "Price and moving averages are aligned upward."
    elif bearish and slope < -flat:
        stage, label, reason = 4, "Stage 4 — Declining", "Price and moving averages are aligned downward."
    elif (price < ma_short and (slope <= flat or ma_short <= ma_medium)) or (
        price >= ma_short * (1 + float(thresholds["extended_above_short_ma_fraction"])) and slope <= flat
    ):
        stage, label, reason = 3, "Stage 3 — Topping / distribution", "The advance is weakening or becoming extended."
    else:
        stage, label, reason = 1, "Stage 1 — Basing / accumulation", "The moving-average structure is not a confirmed advance or decline."
    return {
        "stage": stage,
        "label": label,
        "reason": reason,
        "actual": {"price": price, "ma_short": ma_short, "ma_medium": ma_medium,
                   "ma_long": ma_long, "ma_long_slope": slope,
                   "trend_template_passes": technical_passes},
    }


def run_market_stage_classification(settings: Settings | None = None) -> dict[str, int | str]:
    """Calculate and store the latest daily stage for every active symbol."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        bars = connection.execute(
            """
            SELECT daily.symbol, daily.date,
              coalesce(nullif(daily.adj_close, 0), daily.close) AS close,
              daily.high * coalesce(nullif(daily.adj_close, 0) / nullif(daily.close, 0), 1) AS high,
              daily.low * coalesce(nullif(daily.adj_close, 0) / nullif(daily.close, 0), 1) AS low
            FROM ohlcv_daily daily JOIN universe USING (symbol)
            WHERE universe.is_active = TRUE ORDER BY daily.symbol, daily.date
            """
        ).fetchdf()
        records: list[dict[str, Any]] = []
        for symbol, symbol_bars in bars.groupby("symbol", sort=False):
            trend = calculate_trend_template(symbol_bars, "daily")
            result = classify_market_stage(trend)
            if result["stage"] is None or trend["as_of_date"] is None:
                continue
            actual = result["actual"]
            records.append({"symbol": symbol, "date": trend["as_of_date"], "stage": result["stage"],
                            "price": actual["price"], "ma_short": actual["ma_short"],
                            "ma_medium": actual["ma_medium"], "ma_long": actual["ma_long"],
                            "ma_long_slope": actual["ma_long_slope"],
                            "trend_template_passes": actual["trend_template_passes"]})
        if records:
            staged = pd.DataFrame(records)
            connection.register("staged_market_stages", staged)
            connection.execute(
                """INSERT INTO market_stages_daily AS target SELECT * FROM staged_market_stages
                   ON CONFLICT (symbol, date) DO UPDATE SET
                     stage = excluded.stage, price = excluded.price, ma_short = excluded.ma_short,
                     ma_medium = excluded.ma_medium, ma_long = excluded.ma_long,
                     ma_long_slope = excluded.ma_long_slope,
                     trend_template_passes = excluded.trend_template_passes"""
            )
            connection.unregister("staged_market_stages")
        return {"symbols_classified": len(records), "as_of_date": str(bars["date"].max()) if not bars.empty else ""}
    finally:
        connection.close()


def _load_config() -> dict[str, Any]:
    """Load the configurable stage thresholds."""
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

