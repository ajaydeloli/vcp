"""Weekly-primary VCP detection with daily entry confirmation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

from config.settings import Settings, get_settings
from sepa_scanner.storage.db import initialize_analytics

CONFIG_PATH = Path(__file__).parents[2] / "config" / "vcp.yaml"


def detect_vcp(
    weekly_bars: pd.DataFrame, daily_bars: pd.DataFrame, *, config: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Detect a VCP from weekly structure and confirm it with daily bars.

    Weekly highs and lows form the primary pattern. Daily data only evaluates
    tight closes and a volume-backed breakout at the resulting weekly pivot.
    """
    settings = dict(config) if config is not None else _load_config()
    weekly_settings = settings["weekly"]
    weekly = _normalise_bars(weekly_bars, require_volume=True)
    daily = _normalise_bars(daily_bars, require_volume=True)
    lookback = int(weekly_settings["lookback_weeks"])
    weekly = weekly.tail(lookback).reset_index(drop=True)
    if len(weekly) < 2 * int(weekly_settings["swing_window_weeks"]) + 3:
        return _empty_result("Insufficient weekly history", weekly, daily)

    pivots = _weekly_swings(weekly, int(weekly_settings["swing_window_weeks"]))
    all_contractions = _contractions_from_swings(weekly, pivots)
    contractions = _latest_contracting_sequence(all_contractions, weekly_settings)
    base_weeks = _base_length_weeks(contractions)
    pivot_price = contractions[-1]["start_high"] if contractions else None
    pivot_date = contractions[-1]["start_date"] if contractions else None
    depth_ratios = [item["depth_ratio_to_prior"] for item in contractions[1:]]
    volume_ratios = [item["volume_ratio_to_prior"] for item in contractions[1:]]
    depth_ok = bool(contractions) and all(
        ratio is not None and ratio <= float(weekly_settings["contraction_tolerance"])
        for ratio in depth_ratios
    )
    volume_ok = bool(contractions) and all(
        ratio is not None
        and float(weekly_settings["volume_decay_min"]) <= ratio <= float(weekly_settings["volume_decay_max"])
        for ratio in volume_ratios
    )
    count_ok = int(weekly_settings["min_contractions"]) <= len(contractions) <= int(weekly_settings["max_contractions"])
    base_ok = (
        base_weeks is not None
        and int(weekly_settings["base_length_min_weeks"]) <= base_weeks <= int(weekly_settings["base_length_max_weeks"])
    )
    daily_confirmation = _daily_confirmation(daily, pivot_price, settings["daily_confirmation"])
    is_vcp = count_ok and depth_ok and volume_ok and base_ok
    grade = _quality_grade(is_vcp, count_ok, depth_ok, volume_ok, base_ok, daily_confirmation)
    return {
        "is_vcp": is_vcp,
        "quality_grade": grade,
        "reason": _reason(count_ok, depth_ok, volume_ok, base_ok),
        "as_of_date": _date_value(weekly.iloc[-1]["date"]) if not weekly.empty else None,
        "pivot": {"price": pivot_price, "date": pivot_date},
        "weekly": {
            "bar_count": len(weekly), "swings": pivots, "contractions": contractions,
            "base_length_weeks": base_weeks, "contraction_count": len(contractions),
            "depth_decay_ratios": depth_ratios, "volume_decay_ratios": volume_ratios,
            "depth_decay_passed": depth_ok, "volume_decay_passed": volume_ok,
            "base_length_passed": base_ok,
        },
        "daily_confirmation": daily_confirmation,
        "config": settings,
    }


def detect_symbol_vcp(symbol: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Load one symbol's stored bars and return its latest VCP assessment."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        weekly = connection.execute(
            """SELECT week_end_date AS date, close, high, low, volume
               FROM ohlcv_weekly WHERE symbol = ? ORDER BY week_end_date""", [symbol.upper()]
        ).fetchdf()
        daily = connection.execute(
            """SELECT date, close, high, low, volume
               FROM ohlcv_daily WHERE symbol = ? ORDER BY date""", [symbol.upper()]
        ).fetchdf()
    finally:
        connection.close()
    result = detect_vcp(weekly, daily)
    result["symbol"] = symbol.upper()
    return result


def run_vcp_detection(settings: Settings | None = None) -> dict[str, int | str]:
    """Detect and persist the latest weekly-primary VCP assessment for every active symbol.

    Mirrors `run_market_stage_classification()` / `run_relative_strength_calculation()`:
    fetch adjusted-price bars for the whole active universe in bulk, evaluate
    each symbol with `detect_vcp()`, then upsert one row per symbol into
    `vcp_results_weekly`. Symbols with insufficient weekly history are skipped
    rather than persisted as a false negative.
    """
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        weekly_bars = connection.execute(
            """
            SELECT weekly.symbol, weekly.week_end_date AS date,
              coalesce(nullif(weekly.adj_close, 0), weekly.close) AS close,
              weekly.high * coalesce(nullif(weekly.adj_close, 0) / nullif(weekly.close, 0), 1) AS high,
              weekly.low * coalesce(nullif(weekly.adj_close, 0) / nullif(weekly.close, 0), 1) AS low,
              weekly.volume
            FROM ohlcv_weekly weekly JOIN universe USING (symbol)
            WHERE universe.is_active = TRUE ORDER BY weekly.symbol, weekly.week_end_date
            """
        ).fetchdf()
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
        daily_by_symbol = {symbol: group for symbol, group in daily_bars.groupby("symbol", sort=False)}
        empty_daily = pd.DataFrame(columns=["symbol", "date", "close", "high", "low", "volume"])

        records: list[dict[str, Any]] = []
        for symbol, symbol_weekly in weekly_bars.groupby("symbol", sort=False):
            symbol_daily = daily_by_symbol.get(symbol, empty_daily)
            result = detect_vcp(symbol_weekly, symbol_daily)
            if result["as_of_date"] is None:
                continue
            weekly_info = result["weekly"]
            confirmation = result["daily_confirmation"]
            records.append({
                "symbol": symbol,
                "date": result["as_of_date"],
                "is_vcp": result["is_vcp"],
                "quality_grade": result["quality_grade"],
                "reason": result["reason"],
                "pivot_price": result["pivot"]["price"],
                "pivot_date": result["pivot"]["date"],
                "contraction_count": weekly_info["contraction_count"],
                "base_length_weeks": weekly_info["base_length_weeks"],
                "depth_decay_passed": weekly_info["depth_decay_passed"],
                "volume_decay_passed": weekly_info["volume_decay_passed"],
                "base_length_passed": weekly_info["base_length_passed"],
                "daily_confirmation_available": confirmation.get("available", False),
                "near_pivot": confirmation.get("near_pivot"),
                "distance_to_pivot_fraction": confirmation.get("distance_to_pivot_fraction"),
                "tight_close_count": confirmation.get("tight_close_count"),
                "tight_closes_confirmed": confirmation.get("tight_closes_confirmed"),
                "breakout_confirmed": confirmation.get("breakout_confirmed"),
                "contractions_json": json.dumps(weekly_info["contractions"], default=str),
                "swings_json": json.dumps(weekly_info["swings"], default=str),
            })
        if records:
            staged = pd.DataFrame(records)
            connection.register("staged_vcp_results", staged)
            connection.execute(
                """INSERT INTO vcp_results_weekly AS target SELECT * FROM staged_vcp_results
                   ON CONFLICT (symbol, date) DO UPDATE SET
                     is_vcp = excluded.is_vcp, quality_grade = excluded.quality_grade,
                     reason = excluded.reason, pivot_price = excluded.pivot_price,
                     pivot_date = excluded.pivot_date, contraction_count = excluded.contraction_count,
                     base_length_weeks = excluded.base_length_weeks,
                     depth_decay_passed = excluded.depth_decay_passed,
                     volume_decay_passed = excluded.volume_decay_passed,
                     base_length_passed = excluded.base_length_passed,
                     daily_confirmation_available = excluded.daily_confirmation_available,
                     near_pivot = excluded.near_pivot,
                     distance_to_pivot_fraction = excluded.distance_to_pivot_fraction,
                     tight_close_count = excluded.tight_close_count,
                     tight_closes_confirmed = excluded.tight_closes_confirmed,
                     breakout_confirmed = excluded.breakout_confirmed,
                     contractions_json = excluded.contractions_json,
                     swings_json = excluded.swings_json"""
            )
            connection.unregister("staged_vcp_results")
        return {
            "symbols_evaluated": len(records),
            "symbols_flagged_vcp": sum(1 for record in records if record["is_vcp"]),
            "as_of_date": str(weekly_bars["date"].max()) if not weekly_bars.empty else "",
        }
    finally:
        connection.close()


def latest_vcp_results(settings: Settings | None = None) -> pd.DataFrame:
    """Return the latest persisted VCP assessment for each active symbol."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        return connection.execute(
            """SELECT results.* FROM vcp_results_weekly results
               JOIN universe USING (symbol) WHERE universe.is_active = TRUE
                 AND date = (SELECT max(date) FROM vcp_results_weekly) ORDER BY is_vcp DESC, symbol"""
        ).fetchdf()
    finally:
        connection.close()


def _normalise_bars(bars: pd.DataFrame, *, require_volume: bool) -> pd.DataFrame:
    required = {"date", "close", "high", "low"}
    if require_volume:
        required.add("volume")
    if not required.issubset(bars.columns):
        raise ValueError(f"bars must contain: {', '.join(sorted(required))}")
    frame = bars.copy().sort_values("date").drop_duplicates("date", keep="last")
    for column in required - {"date"}:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=required).query("close > 0 and high > 0 and low > 0 and volume >= 0").reset_index(drop=True)


def _weekly_swings(bars: pd.DataFrame, window: int) -> list[dict[str, Any]]:
    swings: list[dict[str, Any]] = []
    for index in range(window, len(bars) - window):
        segment = bars.iloc[index - window : index + window + 1]
        row = bars.iloc[index]
        if row["high"] == segment["high"].max() and row["high"] > segment["high"].iloc[0] and row["high"] > segment["high"].iloc[-1]:
            swings.append({"type": "high", "index": index, "date": _date_value(row["date"]), "price": float(row["high"])})
        if row["low"] == segment["low"].min() and row["low"] < segment["low"].iloc[0] and row["low"] < segment["low"].iloc[-1]:
            swings.append({"type": "low", "index": index, "date": _date_value(row["date"]), "price": float(row["low"])})
    swings.sort(key=lambda item: item["index"])
    reduced: list[dict[str, Any]] = []
    for swing in swings:
        if reduced and reduced[-1]["type"] == swing["type"]:
            if (swing["type"] == "high" and swing["price"] > reduced[-1]["price"]) or (swing["type"] == "low" and swing["price"] < reduced[-1]["price"]):
                reduced[-1] = swing
        else:
            reduced.append(swing)
    return reduced


def _contractions_from_swings(bars: pd.DataFrame, swings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contractions: list[dict[str, Any]] = []
    for first, second in zip(swings, swings[1:]):
        if first["type"] != "high" or second["type"] != "low":
            continue
        depth = (first["price"] - second["price"]) / first["price"]
        average_volume = float(bars.iloc[first["index"] : second["index"] + 1]["volume"].mean())
        contractions.append({
            "start_date": first["date"], "start_high": first["price"], "low_date": second["date"],
            "low": second["price"], "depth_fraction": depth, "average_volume": average_volume,
        })
    for prior, current in zip(contractions, contractions[1:]):
        current["depth_ratio_to_prior"] = current["depth_fraction"] / prior["depth_fraction"] if prior["depth_fraction"] else None
        current["volume_ratio_to_prior"] = current["average_volume"] / prior["average_volume"] if prior["average_volume"] else None
    if contractions:
        contractions[0]["depth_ratio_to_prior"] = None
        contractions[0]["volume_ratio_to_prior"] = None
    return contractions


def _latest_contracting_sequence(contractions: list[dict[str, Any]], settings: Mapping[str, Any]) -> list[dict[str, Any]]:
    tolerance = float(settings["contraction_tolerance"])
    sequences: list[list[dict[str, Any]]] = []
    for start in range(len(contractions)):
        sequence = [contractions[start]]
        for item in contractions[start + 1:]:
            ratio = item["depth_fraction"] / sequence[-1]["depth_fraction"] if sequence[-1]["depth_fraction"] else None
            if ratio is None or ratio > tolerance:
                break
            item = dict(item)
            item["depth_ratio_to_prior"] = ratio
            item["volume_ratio_to_prior"] = item["average_volume"] / sequence[-1]["average_volume"] if sequence[-1]["average_volume"] else None
            sequence.append(item)
        sequences.append(sequence)
    qualifying = [item for item in sequences if len(item) >= int(settings["min_contractions"])]
    if qualifying:
        return max(qualifying, key=lambda item: item[-1]["start_date"])
    return max(sequences, key=lambda item: item[-1]["start_date"], default=[])


def _base_length_weeks(contractions: list[dict[str, Any]]) -> int | None:
    if len(contractions) < 2:
        return None
    return int((pd.Timestamp(contractions[-1]["low_date"]) - pd.Timestamp(contractions[0]["start_date"])).days // 7 + 1)


def _daily_confirmation(daily: pd.DataFrame, pivot: float | None, settings: Mapping[str, Any]) -> dict[str, Any]:
    if daily.empty or pivot is None:
        return {"available": False, "tight_close_count": 0, "tight_closes_confirmed": False, "breakout_confirmed": False}
    recent = daily.tail(int(settings["lookback_sessions"])).copy()
    latest = recent.iloc[-1]
    anchor = float(latest["close"])
    tight = (recent["close"].sub(anchor).abs() / anchor) <= float(settings["tight_close_tolerance"])
    close_range = (recent["close"].max() - recent["close"].min()) / recent["close"].min()
    close_count = int(tight.sum())
    volume_window = daily.tail(int(settings["breakout_volume_lookback"]) + 1).iloc[:-1]
    average_volume = float(volume_window["volume"].mean()) if not volume_window.empty else None
    breakout = bool(latest["close"] > pivot and average_volume is not None and latest["volume"] >= average_volume * float(settings["breakout_volume_multiple"]))
    return {
        "available": True, "as_of_date": _date_value(latest["date"]), "price": float(latest["close"]),
        "distance_to_pivot_fraction": (float(latest["close"]) - pivot) / pivot,
        "near_pivot": bool(abs(float(latest["close"]) - pivot) / pivot <= float(settings["pivot_proximity"])),
        "tight_close_count": close_count, "tight_close_range_fraction": float(close_range),
        "tight_closes_confirmed": bool(close_count >= int(settings["minimum_tight_closes"]) and close_range <= float(settings["tight_close_tolerance"])),
        "breakout_confirmed": breakout, "latest_volume": float(latest["volume"]), "breakout_average_volume": average_volume,
    }


def _quality_grade(is_vcp: bool, count_ok: bool, depth_ok: bool, volume_ok: bool, base_ok: bool, daily: Mapping[str, Any]) -> str:
    if is_vcp and daily.get("tight_closes_confirmed") and daily.get("near_pivot"):
        return "A+"
    if is_vcp and (daily.get("near_pivot") or daily.get("breakout_confirmed")):
        return "A"
    if count_ok and depth_ok and base_ok:
        return "B"
    return "C"


def _reason(count_ok: bool, depth_ok: bool, volume_ok: bool, base_ok: bool) -> str:
    failed = [name for name, passed in (("contraction count", count_ok), ("depth decay", depth_ok), ("volume dry-up", volume_ok), ("base length", base_ok)) if not passed]
    return "Weekly structure qualifies." if not failed else "Weekly structure needs: " + ", ".join(failed) + "."


def _empty_result(reason: str, weekly: pd.DataFrame, daily: pd.DataFrame) -> dict[str, Any]:
    return {"is_vcp": False, "quality_grade": "C", "reason": reason, "as_of_date": None,
            "pivot": {"price": None, "date": None}, "weekly": {"bar_count": len(weekly), "swings": [], "contractions": []},
            "daily_confirmation": {"available": not daily.empty, "tight_close_count": 0, "tight_closes_confirmed": False, "breakout_confirmed": False}}


def _date_value(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _load_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

