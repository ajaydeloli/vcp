"""Universe-wide RS ratings and relative-strength line history."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

from config.settings import Settings, get_settings
from sepa_scanner.storage.db import initialize_analytics

CONFIG_PATH = Path(__file__).parents[2] / "config" / "relative_strength.yaml"


def calculate_relative_strength_history(
    bars: pd.DataFrame, *, config: Mapping[str, Any] | None = None
) -> pd.DataFrame:
    """Calculate daily 1–99 ratings and the equal-weight-relative RS line.

    Each rating uses only prices on or before its own date. A row is emitted
    only after all four configured return windows are available.
    """
    settings = dict(config) if config is not None else _load_config()
    if not {"symbol", "date", "close"}.issubset(bars.columns):
        raise ValueError("bars must contain symbol, date, and close columns")
    lookbacks = settings["lookbacks"]
    weights = settings["weights"]
    if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-9:
        raise ValueError("relative-strength weights must sum to 1")
    frame = bars[["symbol", "date", "close"]].copy()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna().query("close > 0").sort_values(["date", "symbol"])
    prices = frame.pivot(index="date", columns="symbol", values="close").sort_index()
    components = {
        name: prices / prices.shift(int(lookbacks[f"{name}_sessions"])) - 1
        for name in ("three_month", "six_month", "nine_month", "twelve_month")
    }
    composite = sum(components[name] * float(weights[name]) for name in components)
    # The benchmark is an equal-weight index of observed daily stock returns.
    benchmark = (1 + prices.pct_change().mean(axis=1, skipna=True).fillna(0)).cumprod()
    rs_line = prices.div(benchmark, axis=0)
    prior_rs_high = rs_line.shift().cummax()
    prior_price_high = prices.shift().cummax()
    new_high_before_price = (rs_line >= prior_rs_high) & (prices < prior_price_high)

    history = composite.stack().rename("composite_return").reset_index()
    history.columns = ["date", "symbol", "composite_return"]
    rankings = composite.rank(axis=1, method="average", pct=True).mul(98).add(1).round().astype("Int64")
    ratings = rankings.stack().rename("rating").reset_index()
    ratings.columns = ["date", "symbol", "rating"]
    lines = rs_line.stack().rename("rs_line").reset_index()
    lines.columns = ["date", "symbol", "rs_line"]
    flags = new_high_before_price.stack().rename("rs_line_new_high").reset_index()
    flags.columns = ["date", "symbol", "rs_line_new_high"]
    result = history.merge(ratings, on=["date", "symbol"]).merge(lines, on=["date", "symbol"]).merge(flags, on=["date", "symbol"])
    result = result.dropna(subset=["composite_return", "rating", "rs_line"])
    result["universe_size"] = result.groupby("date")["symbol"].transform("size")
    result["rating"] = result["rating"].astype(int)
    result["rs_line_new_high"] = result["rs_line_new_high"].fillna(False).astype(bool)
    return result[["symbol", "date", "composite_return", "rating", "rs_line", "rs_line_new_high", "universe_size"]]


def run_relative_strength_calculation(settings: Settings | None = None) -> dict[str, int | str]:
    """Calculate and persist RS history for all active symbols."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        bars = connection.execute(
            """SELECT daily.symbol, daily.date,
                      coalesce(nullif(daily.adj_close, 0), daily.close) AS close
               FROM ohlcv_daily daily JOIN universe USING (symbol)
               WHERE universe.is_active = TRUE ORDER BY daily.date, daily.symbol"""
        ).fetchdf()
        history = calculate_relative_strength_history(bars)
        if history.empty:
            return {"rows_persisted": 0, "symbols_rated": 0, "as_of_date": ""}
        connection.register("staged_rs_ratings", history)
        connection.execute(
            """INSERT INTO rs_ratings_daily AS target SELECT * FROM staged_rs_ratings
               ON CONFLICT (symbol, date) DO UPDATE SET
                 composite_return = excluded.composite_return, rating = excluded.rating,
                 rs_line = excluded.rs_line, rs_line_new_high = excluded.rs_line_new_high,
                 universe_size = excluded.universe_size"""
        )
        connection.unregister("staged_rs_ratings")
        latest = history[history["date"] == history["date"].max()]
        return {"rows_persisted": len(history), "symbols_rated": latest["symbol"].nunique(),
                "as_of_date": str(history["date"].max())}
    finally:
        connection.close()


def latest_relative_strength_ratings(settings: Settings | None = None) -> pd.DataFrame:
    """Return the latest persisted rating for each active symbol."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        return connection.execute(
            """SELECT ratings.* FROM rs_ratings_daily ratings
               JOIN universe USING (symbol) WHERE universe.is_active = TRUE
                 AND date = (SELECT max(date) FROM rs_ratings_daily) ORDER BY rating DESC, symbol"""
        ).fetchdf()
    finally:
        connection.close()


def _load_config() -> dict[str, Any]:
    """Load RS windows and weights from the project configuration."""
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

