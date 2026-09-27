"""Supply/demand and volume signal calculations.

Three signals from design doc §6.5, each returning measured values alongside
any pass/fail flag so results stay explainable rather than opaque booleans:

- Up/down volume ratio over a lookback window.
- An accumulation/distribution (Chaikin A/D) proxy and its recent trend.
- A volume-spike-on-an-up-day near a reference "pivot" price -- an
  institutional-footprint signal. The pivot is typically the symbol's latest
  persisted VCP pivot (see `run_volume_signals`), but the pure function accepts
  any reference price, since "near a pivot" is a generally useful technique
  (e.g. near a 52-week high) independent of an active VCP setup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

from config.settings import Settings, get_settings
from sepa_scanner.storage.db import initialize_analytics

CONFIG_PATH = Path(__file__).parents[2] / "config" / "volume_signals.yaml"


def calculate_volume_signals(
    bars: pd.DataFrame, *, pivot_price: float | None = None, config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Calculate volume/supply-demand signals from one symbol's daily bars.

    Input bars must contain `date`, `close`, `high`, `low`, and `volume`
    columns. `close`/`high`/`low` should be adjusted for corporate actions
    when adjusted data is available, matching the other analytics modules.
    Insufficient history yields `sufficient_history: False` / `None` values
    rather than a misleading zero or false.
    """
    settings = dict(config) if config is not None else _load_config()
    frame = _normalise_daily_bars(bars)
    as_of_date = _date_value(frame.iloc[-1]["date"]) if not frame.empty else None
    return {
        "as_of_date": as_of_date,
        "bar_count": len(frame),
        "up_down_volume": _up_down_volume(frame, settings["up_down_volume"]),
        "accumulation_distribution": _accumulation_distribution(frame, settings["accumulation_distribution"]),
        "pivot_volume_spike": _pivot_volume_spike(frame, pivot_price, settings["pivot_volume_spike"]),
    }


def calculate_symbol_volume_signals(
    symbol: str, *, pivot_price: float | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Load one symbol's stored daily bars and return its latest volume signals."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        rows = connection.execute(
            """
            SELECT date,
              coalesce(nullif(adj_close, 0), close) AS close,
              high * coalesce(nullif(adj_close, 0) / nullif(close, 0), 1) AS high,
              low * coalesce(nullif(adj_close, 0) / nullif(close, 0), 1) AS low,
              volume
            FROM ohlcv_daily WHERE symbol = ? ORDER BY date
            """,
            [symbol.upper()],
        ).fetchdf()
        if pivot_price is None:
            pivot_row = connection.execute(
                """SELECT pivot_price FROM vcp_results_weekly
                   WHERE symbol = ? AND pivot_price IS NOT NULL
                   ORDER BY date DESC LIMIT 1""",
                [symbol.upper()],
            ).fetchone()
            if pivot_row is not None:
                pivot_price = float(pivot_row[0])
    finally:
        connection.close()
    result = calculate_volume_signals(rows, pivot_price=pivot_price)
    result["symbol"] = symbol.upper()
    return result


def run_volume_signals(settings: Settings | None = None) -> dict[str, int | str]:
    """Calculate and persist the latest volume signals for every active symbol.

    Mirrors `run_vcp_detection()` / `run_market_stage_classification()`:
    bulk-fetch adjusted daily bars for the active universe, look up each
    symbol's latest persisted VCP pivot (if any) from `vcp_results_weekly`,
    evaluate, and upsert one row per symbol into `volume_signals_daily`.
    """
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        daily_bars = connection.execute(
            """
            SELECT daily.symbol, daily.date,
              coalesce(nullif(daily.adj_close, 0), daily.close) AS close,
              daily.high * coalesce(nullif(daily.adj_close, 0) / nullif(daily.close, 0), 1) AS high,
              daily.low * coalesce(nullif(daily.adj_close, 0) / nullif(daily.close, 0), 1) AS low,
              daily.volume
            FROM ohlcv_daily daily JOIN universe USING (symbol)
            WHERE universe.is_active = TRUE ORDER BY daily.symbol, daily.date
            """
        ).fetchdf()
        pivots = connection.execute(
            """SELECT symbol, pivot_price FROM vcp_results_weekly
               WHERE date = (SELECT max(date) FROM vcp_results_weekly) AND pivot_price IS NOT NULL"""
        ).fetchdf()
        pivot_by_symbol = dict(zip(pivots["symbol"], pivots["pivot_price"]))

        records: list[dict[str, Any]] = []
        for symbol, symbol_bars in daily_bars.groupby("symbol", sort=False):
            result = calculate_volume_signals(symbol_bars, pivot_price=pivot_by_symbol.get(symbol))
            if result["as_of_date"] is None:
                continue
            up_down = result["up_down_volume"]
            accumulation = result["accumulation_distribution"]
            spike = result["pivot_volume_spike"]
            records.append({
                "symbol": symbol,
                "date": result["as_of_date"],
                "up_down_volume_ratio": up_down["ratio"],
                "up_volume": up_down["up_volume"],
                "down_volume": up_down["down_volume"],
                "accumulation_distribution_value": accumulation["current_value"],
                "accumulation_distribution_rising": accumulation["trend_rising"],
                "pivot_price_used": spike["pivot_price"],
                "volume_spike_near_pivot": spike["spike_detected"],
                "spike_date": spike["spike_date"],
                "spike_volume": spike["spike_volume"],
            })
        if records:
            staged = pd.DataFrame(records)
            connection.register("staged_volume_signals", staged)
            connection.execute(
                """INSERT INTO volume_signals_daily AS target SELECT * FROM staged_volume_signals
                   ON CONFLICT (symbol, date) DO UPDATE SET
                     up_down_volume_ratio = excluded.up_down_volume_ratio,
                     up_volume = excluded.up_volume, down_volume = excluded.down_volume,
                     accumulation_distribution_value = excluded.accumulation_distribution_value,
                     accumulation_distribution_rising = excluded.accumulation_distribution_rising,
                     pivot_price_used = excluded.pivot_price_used,
                     volume_spike_near_pivot = excluded.volume_spike_near_pivot,
                     spike_date = excluded.spike_date, spike_volume = excluded.spike_volume"""
            )
            connection.unregister("staged_volume_signals")
        return {
            "symbols_evaluated": len(records),
            "symbols_with_pivot_spike": sum(1 for record in records if record["volume_spike_near_pivot"]),
            "as_of_date": str(daily_bars["date"].max()) if not daily_bars.empty else "",
        }
    finally:
        connection.close()


def latest_volume_signals(settings: Settings | None = None) -> pd.DataFrame:
    """Return the latest persisted volume signals for each active symbol."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        return connection.execute(
            """SELECT signals.* FROM volume_signals_daily signals
               JOIN universe USING (symbol) WHERE universe.is_active = TRUE
                 AND date = (SELECT max(date) FROM volume_signals_daily) ORDER BY symbol"""
        ).fetchdf()
    finally:
        connection.close()


def _normalise_daily_bars(bars: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "close", "high", "low", "volume"}
    if not required.issubset(bars.columns):
        raise ValueError(f"bars must contain columns: {', '.join(sorted(required))}")
    frame = bars.copy().sort_values("date").drop_duplicates("date", keep="last")
    for column in required - {"date"}:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=required)
    return frame.query("close > 0 and high > 0 and low > 0 and volume >= 0").reset_index(drop=True)


def _up_down_volume(frame: pd.DataFrame, settings: Mapping[str, Any]) -> dict[str, Any]:
    lookback = int(settings["lookback_sessions"])
    if len(frame) < lookback + 1:
        return {
            "lookback_sessions": lookback, "up_volume": None, "down_volume": None,
            "ratio": None, "sufficient_history": False,
        }
    recent = frame.tail(lookback + 1).copy()  # +1 so the earliest bar still has a prior close to diff
    change = recent["close"].diff()
    up_volume = float(recent.loc[change > 0, "volume"].sum())
    down_volume = float(recent.loc[change < 0, "volume"].sum())
    ratio = up_volume / down_volume if down_volume > 0 else None
    return {
        "lookback_sessions": lookback, "up_volume": up_volume, "down_volume": down_volume,
        "ratio": ratio, "sufficient_history": True,
    }


def _accumulation_distribution(frame: pd.DataFrame, settings: Mapping[str, Any]) -> dict[str, Any]:
    lookback = int(settings["lookback_sessions"])
    if frame.empty:
        return {
            "lookback_sessions": lookback, "current_value": None,
            "value_lookback_sessions_ago": None, "trend_rising": None, "sufficient_history": False,
        }
    high_low_range = frame["high"] - frame["low"]
    money_flow_multiplier = (
        ((frame["close"] - frame["low"]) - (frame["high"] - frame["close"])).div(high_low_range)
    ).where(high_low_range != 0, 0.0)
    ad_line = (money_flow_multiplier * frame["volume"]).cumsum()
    current_value = float(ad_line.iloc[-1])
    sufficient = len(frame) > lookback
    prior_value = float(ad_line.iloc[-1 - lookback]) if sufficient else None
    trend_rising = (current_value > prior_value) if prior_value is not None else None
    return {
        "lookback_sessions": lookback, "current_value": current_value,
        "value_lookback_sessions_ago": prior_value, "trend_rising": trend_rising,
        "sufficient_history": sufficient,
    }


def _pivot_volume_spike(
    frame: pd.DataFrame, pivot_price: float | None, settings: Mapping[str, Any]
) -> dict[str, Any]:
    lookback = int(settings["lookback_sessions"])
    proximity = float(settings["proximity_fraction"])
    volume_lookback = int(settings["volume_average_lookback"])
    multiple = float(settings["volume_multiple"])
    result: dict[str, Any] = {
        "available": pivot_price is not None, "pivot_price": pivot_price,
        "lookback_sessions": lookback, "proximity_fraction": proximity, "volume_multiple": multiple,
        "spike_detected": False, "spike_date": None, "spike_volume": None, "spike_average_volume": None,
    }
    if pivot_price is None or len(frame) < volume_lookback + 1:
        return result
    candidate_positions = frame.index[-lookback:] if len(frame) >= lookback else frame.index
    best: dict[str, Any] | None = None
    for position in candidate_positions:
        if position < volume_lookback:
            continue
        row = frame.loc[position]
        prior_close = frame.loc[position - 1, "close"]
        is_up_day = row["close"] > prior_close
        near_pivot = abs(row["close"] - pivot_price) / pivot_price <= proximity
        if not (is_up_day and near_pivot):
            continue
        window_average = float(frame.loc[position - volume_lookback : position - 1, "volume"].mean())
        if window_average <= 0 or row["volume"] < window_average * multiple:
            continue
        if best is None or row["volume"] > best["spike_volume"]:
            best = {
                "spike_date": _date_value(row["date"]), "spike_volume": float(row["volume"]),
                "spike_average_volume": window_average,
            }
    if best is not None:
        result.update(best)
        result["spike_detected"] = True
    return result


def _date_value(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _load_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
