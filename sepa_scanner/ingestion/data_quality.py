"""Post-ingestion data-quality checks for the local analytical store."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings
from sepa_scanner.storage.db import initialize_analytics


def run_data_quality_validation(settings: Settings | None = None) -> dict[str, Any]:
    """Validate full-universe daily and weekly data, then persist a JSON report."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics()
    try:
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "daily_rows": connection.execute("SELECT count(*) FROM ohlcv_daily").fetchone()[0],
            "weekly_rows": connection.execute("SELECT count(*) FROM ohlcv_weekly").fetchone()[0],
            "symbols_with_daily_data_and_listing_date": connection.execute(
                """SELECT count(*) FROM (
                     SELECT DISTINCT daily.symbol FROM ohlcv_daily daily
                     JOIN universe USING (symbol) WHERE universe.listing_date IS NOT NULL
                   )"""
            ).fetchone()[0],
            "symbols_with_daily_data_missing_listing_date": connection.execute(
                """SELECT count(*) FROM (
                     SELECT DISTINCT daily.symbol FROM ohlcv_daily daily
                     LEFT JOIN universe USING (symbol)
                     WHERE universe.listing_date IS NULL
                   )"""
            ).fetchone()[0],
            "invalid_ohlc_rows": connection.execute(
                """SELECT count(*) FROM ohlcv_daily
                WHERE low > least(open, close) OR high < greatest(open, close)"""
            ).fetchone()[0],
            "negative_price_or_volume_rows": connection.execute(
                """SELECT count(*) FROM ohlcv_daily
                WHERE least(open, high, low, close) < 0 OR volume < 0"""
            ).fetchone()[0],
            "zero_volume_rows": connection.execute(
                "SELECT count(*) FROM ohlcv_daily WHERE volume = 0"
            ).fetchone()[0],
            "weekly_reconciliation_mismatches": connection.execute(
                """
                WITH derived AS (
                  SELECT symbol,
                    CAST(date_trunc('week', date) + INTERVAL 4 DAY AS DATE) AS week_end_date,
                    arg_min(open, date) AS open, max(high) AS high, min(low) AS low,
                    arg_max(close, date) AS close, sum(volume) AS volume
                  FROM ohlcv_daily GROUP BY symbol, date_trunc('week', date)
                )
                SELECT count(*) FROM derived daily
                FULL OUTER JOIN ohlcv_weekly weekly USING (symbol, week_end_date)
                WHERE daily.open IS DISTINCT FROM weekly.open
                   OR daily.high IS DISTINCT FROM weekly.high
                   OR daily.low IS DISTINCT FROM weekly.low
                   OR daily.close IS DISTINCT FROM weekly.close
                   OR daily.volume IS DISTINCT FROM weekly.volume
                """
            ).fetchone()[0],
            "missing_observed_market_dates": connection.execute(
                """
                WITH market_days AS (SELECT DISTINCT date FROM ohlcv_daily),
                coverage AS (
                  SELECT symbol, min(date) AS first_date, max(date) AS last_date
                  FROM ohlcv_daily GROUP BY symbol
                ), expected_coverage AS (
                  SELECT coverage.symbol, coverage.first_date, coverage.last_date,
                    greatest(coverage.first_date,
                      coalesce(universe.listing_date, coverage.first_date)) AS expected_from
                  FROM coverage
                  LEFT JOIN universe USING (symbol)
                )
                SELECT count(*) FROM expected_coverage coverage
                JOIN market_days market_day
                  ON market_day.date BETWEEN coverage.expected_from AND coverage.last_date
                LEFT JOIN ohlcv_daily daily
                  ON daily.symbol = coverage.symbol AND daily.date = market_day.date
                WHERE daily.date IS NULL
                """
            ).fetchone()[0],
            "missing_date_review_flags": [
                {
                    "symbol": row[0],
                    "listing_date": row[1].isoformat() if row[1] else None,
                    "first_observed_date": row[2].isoformat(),
                    "last_observed_date": row[3].isoformat(),
                    "missing_dates": [date.isoformat() for date in row[4]],
                    "missing_date_count": len(row[4]),
                }
                for row in connection.execute(
                    """
                    WITH market_days AS (SELECT DISTINCT date FROM ohlcv_daily),
                    coverage AS (
                      SELECT symbol, min(date) AS first_date, max(date) AS last_date
                      FROM ohlcv_daily GROUP BY symbol
                    ), expected_coverage AS (
                      SELECT coverage.symbol, coverage.first_date, coverage.last_date,
                        universe.listing_date,
                        greatest(coverage.first_date,
                          coalesce(universe.listing_date, coverage.first_date)) AS expected_from
                      FROM coverage
                      LEFT JOIN universe USING (symbol)
                    )
                    SELECT coverage.symbol, coverage.listing_date,
                      coverage.first_date, coverage.last_date,
                      list(market_day.date ORDER BY market_day.date) AS missing_dates
                    FROM expected_coverage coverage
                    JOIN market_days market_day
                      ON market_day.date BETWEEN coverage.expected_from AND coverage.last_date
                    LEFT JOIN ohlcv_daily daily
                      ON daily.symbol = coverage.symbol AND daily.date = market_day.date
                    WHERE daily.date IS NULL
                    GROUP BY coverage.symbol, coverage.listing_date,
                      coverage.first_date, coverage.last_date
                    ORDER BY coverage.symbol
                    """
                ).fetchall()
            ],
            "stale_close_runs_over_10_sessions": connection.execute(
                """
                WITH marks AS (
                  SELECT symbol, date, close,
                    CASE WHEN close IS DISTINCT FROM lag(close) OVER (PARTITION BY symbol ORDER BY date)
                      THEN 1 ELSE 0 END AS changed
                  FROM ohlcv_daily
                ), grouped AS (
                  SELECT symbol, date, close,
                    sum(changed) OVER (PARTITION BY symbol ORDER BY date) AS group_id
                  FROM marks
                )
                SELECT count(*) FROM (
                  SELECT symbol, group_id FROM grouped
                  GROUP BY symbol, group_id HAVING count(*) > 10
                )
                """
            ).fetchone()[0],
        }
    finally:
        connection.close()

    output_dir = runtime_settings.data_directory / "quality"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"quality-{datetime.now(timezone.utc).date().isoformat()}.json"
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(output_path)
    return report
