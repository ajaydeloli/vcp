"""Idempotent quarterly fundamentals ingestion.

Calls a FundamentalsProvider -- ScreenerProvider in production as of
2026-09-27 (see sepa_scanner/ingestion/providers/factory.py's
create_fundamentals_provider(), which reads settings.fundamentals.primary_source)
-- per active symbol, upserts normalized filings into fundamentals_quarterly,
and computes EPS/sales YoY growth at ingestion time via a self-join against
each symbol's own row from ~4 quarters prior -- so
sepa_scanner/scoring/scorer.py's future _score_fundamentals() can stay a
pure lookup like every other component, never recomputing growth itself.
This job itself is provider-agnostic (it only depends on the
FundamentalsProvider Protocol), so the primary/fallback switch lives
entirely in which provider the caller passes in, not in this file.

Mirrors sepa_scanner/ingestion/daily_update.py's idempotent pattern:
delta-fetch per symbol based on what's already stored, bulk upsert via a
staged DataFrame with ON CONFLICT (symbol, quarter_end) DO UPDATE, one
ingestion_log row per run, raw (normalized) filings archived under
data/raw/<run-id>/ the same way daily_update.py archives normalized bars.

Two confirmed edge cases (PROJECT-CONTEXT.md) are handled explicitly, not
silently:
  - fields_missing=True (banks/NBFCs, different XBRL taxonomy): that
    quarter's eps/sales are already None from the provider, and growth
    involving either the current or the prior-year quarter is forced NULL,
    never 0.
  - A backlog filing (announcement_date far past period_end, e.g. the real
    AHLWEST case -- broadcast 5+ years late): still stored, but flagged via
    announcement_lag_days / is_backlog_filing so a future point-in-time
    consumer doesn't treat it as current.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pandas as pd

from config.settings import Settings, get_settings
from sepa_scanner.ingestion.providers.fundamentals_base import FundamentalsProvider, QuarterlyFiling
from sepa_scanner.storage.db import initialize_analytics

# YoY comparison window: "~4 quarters prior" with tolerance for filers whose
# quarter-end dates drift slightly year over year. 320-410 days brackets
# 365 +/- ~45 days -- wide enough for real-world filing-date jitter, narrow
# enough that it can't accidentally match 2 or 6 quarters back.
_YOY_MIN_DAYS = 320
_YOY_MAX_DAYS = 410


def _validate_filing(filing: QuarterlyFiling) -> str | None:
    """Return a rejection reason, or None if the filing is structurally sane.

    Deliberately does NOT reject on missing revenue/PAT/EPS (fields_missing
    covers that, and it's a real, expected data-quality state, not a broken
    record) or on negative PAT/EPS (a genuine loss-making quarter is valid
    data). Only rejects filings whose own dates are internally inconsistent.
    """
    if filing.period_start > filing.period_end:
        return "period_start is after period_end"
    if filing.announcement_date < filing.period_end:
        # Announcing a quarter before it has even ended is not a backlog
        # filing (that's the opposite problem) -- it means a date field was
        # parsed wrong, so this is rejected rather than flagged.
        return "announcement_date is before period_end"
    return None


def _select_preferred_filings(filings: list[QuarterlyFiling]) -> list[QuarterlyFiling]:
    """A single fetch_quarterly() call can return both a consolidated and a
    standalone filing for the same quarter -- confirmed live for NSE's
    filing index (2026-09-27 validation run against
    RELIANCE/INFY/TCS/HDFCBANK/ICICIBANK: RELIANCE's raw fetch included 2
    rows per period_end with materially different EPS/revenue, e.g. FY25
    Q2 consolidated EPS 24.48 vs standalone 11.40) and applies equally to
    ScreenerProvider, which can likewise be asked to prefer consolidated
    and fall back to standalone per company. Both rows share the same
    (symbol, quarter_end) primary key, so storing both as separate upserts
    would let whichever happened to be processed last silently overwrite
    the other -- exactly the order-dependent corruption this project's
    ingestion rules exist to prevent. Consolidated is kept when both exist
    (SEPA-style growth/quality reads whole-group financials, not just the
    standalone parent entity); standalone is kept only when no consolidated
    filing exists for that period.
    """
    preferred: dict[date, QuarterlyFiling] = {}
    for filing in filings:
        existing = preferred.get(filing.period_end)
        if existing is None or (filing.is_consolidated and not existing.is_consolidated):
            preferred[filing.period_end] = filing
    return [preferred[period_end] for period_end in sorted(preferred)]


def _serialize_filing(filing: QuarterlyFiling) -> dict:
    """Normalized (not vendor-raw) archive record, mirroring daily_update.py's
    asdict(bar) | {"date": ...} pattern for OHLCV bars."""
    payload = asdict(filing)
    for key in ("period_start", "period_end", "announcement_date"):
        payload[key] = payload[key].isoformat()
    return payload


def _recompute_yoy_growth(connection: object, symbols: list[str]) -> None:
    """Recompute eps_yoy_growth/sales_yoy_growth for every stored quarter of
    `symbols`, via self-join against the full local table -- not just rows
    touched in this run. This matters both ways: a newly-arrived quarter
    needs its own growth computed against an older stored quarter, and a
    late-arriving prior-year restatement should correct a quarter already
    on file. Reset-then-recompute (rather than a single UPDATE...FROM) so a
    quarter that no longer has a qualifying prior-year match ends up NULL,
    not stale.
    """
    if not symbols:
        return
    connection.execute(
        """
        UPDATE fundamentals_quarterly
        SET eps_yoy_growth = NULL, sales_yoy_growth = NULL
        WHERE symbol IN (SELECT * FROM UNNEST(?))
        """,
        [symbols],
    )
    connection.execute(
        f"""
        WITH prior_match AS (
          SELECT
            current.symbol, current.quarter_end,
            prior.eps AS prior_eps, prior.sales AS prior_sales,
            prior.fields_missing AS prior_fields_missing
          FROM fundamentals_quarterly AS current
          JOIN fundamentals_quarterly AS prior
            ON prior.symbol = current.symbol
           AND prior.quarter_end BETWEEN
                 current.quarter_end - INTERVAL '{_YOY_MAX_DAYS} DAY'
                 AND current.quarter_end - INTERVAL '{_YOY_MIN_DAYS} DAY'
          WHERE current.symbol IN (SELECT * FROM UNNEST(?))
          QUALIFY row_number() OVER (
            PARTITION BY current.symbol, current.quarter_end
            ORDER BY abs(date_diff('day', prior.quarter_end, current.quarter_end - INTERVAL 365 DAY))
          ) = 1
        )
        UPDATE fundamentals_quarterly AS target
        SET
          eps_yoy_growth = CASE
            WHEN target.fields_missing OR prior_match.prior_fields_missing
              OR target.eps IS NULL OR prior_match.prior_eps IS NULL OR prior_match.prior_eps = 0
            THEN NULL
            ELSE (target.eps - prior_match.prior_eps) / abs(prior_match.prior_eps) * 100
          END,
          sales_yoy_growth = CASE
            WHEN target.fields_missing OR prior_match.prior_fields_missing
              OR target.sales IS NULL OR prior_match.prior_sales IS NULL OR prior_match.prior_sales = 0
            THEN NULL
            ELSE (target.sales - prior_match.prior_sales) / abs(prior_match.prior_sales) * 100
          END
        FROM prior_match
        WHERE target.symbol = prior_match.symbol AND target.quarter_end = prior_match.quarter_end
        """,
        [symbols],
    )


def run_fundamentals_update(
    provider: FundamentalsProvider,
    *,
    settings: Settings | None = None,
    symbols: set[str] | None = None,
    end_date: date | None = None,
    initial_start_date: date = date(2018, 1, 1),
) -> dict[str, int | str]:
    """Fetch new quarterly filings for all active symbols (or a controlled
    subset) and upsert them into fundamentals_quarterly.

    Delta-fetch is keyed on announcement_date (what NSE's own endpoint
    windows on -- see NSEXBRLProvider), not quarter_end: for each symbol,
    fetch from the day after its latest stored announcement_date through
    `end_date` (default today). A symbol with no stored history yet is
    fetched from `initial_start_date`, mirroring run_daily_update()'s
    OHLCV backfill default. YoY growth is recomputed after the upsert
    against the FULL local table for every touched symbol (see
    _recompute_yoy_growth), not just rows fetched in this run.
    """
    runtime_settings = settings or get_settings()
    run_id = str(uuid4())
    now = datetime.now(timezone.utc)
    latest_date = end_date or date.today()
    backlog_threshold_days = runtime_settings.fundamentals.backlog_lag_threshold_days
    # Explicit path (not the bare initialize_analytics() daily_update.py
    # uses) so a caller-supplied `settings` actually redirects storage --
    # matching run_volume_signals()/run_vcp_detection()/run_scoring(), and
    # required for this module's tests to run against an isolated tmp_path
    # DB rather than the real local store.
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    raw_directory = runtime_settings.data_directory / "raw" / run_id
    updated_symbols: list[str] = []
    failed_symbols = 0
    failure_notes: list[str] = []
    rows_added = 0

    try:
        symbol_rows = connection.execute(
            "SELECT symbol FROM universe WHERE is_active = TRUE ORDER BY symbol"
        ).fetchall()
        active_symbols = [row[0] for row in symbol_rows]
        if symbols is not None:
            active_symbols = [symbol for symbol in active_symbols if symbol in symbols]

        staged_records: list[dict] = []
        for symbol in active_symbols:
            last_announced = connection.execute(
                "SELECT max(announcement_date) FROM fundamentals_quarterly WHERE symbol = ?", [symbol]
            ).fetchone()[0]
            from_date = last_announced + timedelta(days=1) if last_announced else initial_start_date
            if from_date > latest_date:
                continue
            try:
                filings = provider.fetch_quarterly(symbol, from_date, latest_date)
            except Exception as error:
                failed_symbols += 1
                failure_notes.append(f"{symbol}: {type(error).__name__}: {str(error)[:160]}")
                continue

            if not filings:
                continue

            # Archive everything fetched (consolidated + standalone both,
            # if NSE returned both) before deduplicating -- the raw archive
            # is the audit trail a parsing/selection bug gets replayed
            # against, so it should hold what was actually received.
            raw_directory.mkdir(parents=True, exist_ok=True)
            (raw_directory / f"{symbol}.json").write_text(
                json.dumps([_serialize_filing(filing) for filing in filings]), encoding="utf-8"
            )

            symbol_had_valid_filing = False
            for filing in _select_preferred_filings(filings):
                rejection_reason = _validate_filing(filing)
                if rejection_reason is not None:
                    failed_symbols += 1
                    failure_notes.append(
                        f"{symbol} {filing.period_end.isoformat()}: rejected ({rejection_reason})"
                    )
                    continue
                lag_days = (filing.announcement_date - filing.period_end).days
                staged_records.append({
                    "symbol": filing.symbol,
                    "quarter_end": filing.period_end,
                    "period_start": filing.period_start,
                    "announcement_date": filing.announcement_date,
                    "eps": filing.basic_eps,
                    "sales": filing.revenue,
                    "roe": None,
                    "eps_yoy_growth": None,
                    "sales_yoy_growth": None,
                    "source": filing.source,
                    "fields_missing": filing.fields_missing,
                    "announcement_lag_days": lag_days,
                    "is_backlog_filing": lag_days > backlog_threshold_days,
                    "announcement_date_is_estimated": filing.announcement_date_is_estimated,
                    "ingested_at": now,
                })
                rows_added += 1
                symbol_had_valid_filing = True
            if symbol_had_valid_filing:
                updated_symbols.append(symbol)

        if staged_records:
            staged = pd.DataFrame(staged_records)
            connection.register("staged_fundamentals", staged)
            try:
                # Explicit column lists on both sides (not SELECT * / a
                # positional INSERT) so this stays correct regardless of
                # physical column order in fundamentals_quarterly -- which
                # can legitimately differ between a freshly created table
                # (schema.sql's CREATE TABLE order) and an existing
                # production table that picked up a later column via
                # ALTER TABLE ... ADD COLUMN (which always appends at the
                # end). A positional SELECT * would silently misalign
                # columns the moment those two orders diverge.
                connection.execute(
                    """
                    INSERT INTO fundamentals_quarterly AS target (
                      symbol, quarter_end, period_start, announcement_date, eps, sales, roe,
                      eps_yoy_growth, sales_yoy_growth, source, fields_missing,
                      announcement_lag_days, is_backlog_filing, announcement_date_is_estimated,
                      ingested_at
                    )
                    SELECT
                      symbol, quarter_end, period_start, announcement_date, eps, sales, roe,
                      eps_yoy_growth, sales_yoy_growth, source, fields_missing,
                      announcement_lag_days, is_backlog_filing, announcement_date_is_estimated,
                      ingested_at
                    FROM staged_fundamentals
                    ON CONFLICT (symbol, quarter_end) DO UPDATE SET
                      period_start = excluded.period_start,
                      announcement_date = excluded.announcement_date,
                      eps = excluded.eps, sales = excluded.sales, roe = excluded.roe,
                      source = excluded.source, fields_missing = excluded.fields_missing,
                      announcement_lag_days = excluded.announcement_lag_days,
                      is_backlog_filing = excluded.is_backlog_filing,
                      announcement_date_is_estimated = excluded.announcement_date_is_estimated,
                      ingested_at = excluded.ingested_at
                    """
                )
            finally:
                connection.unregister("staged_fundamentals")
            _recompute_yoy_growth(connection, sorted(updated_symbols))

        connection.execute(
            "INSERT INTO ingestion_log VALUES (?, ?, ?, ?, ?, ?)",
            [
                run_id, now, len(updated_symbols), failed_symbols, rows_added,
                "fundamentals update" + ("; " + " | ".join(failure_notes) if failure_notes else ""),
            ],
        )
    finally:
        connection.close()

    return {
        "run_id": run_id,
        "symbols_updated": len(updated_symbols),
        "symbols_failed": failed_symbols,
        "rows_added": rows_added,
    }


def latest_fundamentals(settings: Settings | None = None) -> pd.DataFrame:
    """Return each active symbol's most recent stored quarterly filing,
    mirroring the other analytics modules' latest_*() read helpers."""
    runtime_settings = settings or get_settings()
    connection = initialize_analytics(runtime_settings.data_directory / "market.duckdb")
    try:
        return connection.execute(
            """
            SELECT fundamentals.*
            FROM fundamentals_quarterly fundamentals
            JOIN universe USING (symbol)
            WHERE universe.is_active = TRUE
              AND quarter_end = (
                SELECT max(quarter_end) FROM fundamentals_quarterly latest
                WHERE latest.symbol = fundamentals.symbol
              )
            ORDER BY symbol
            """
        ).fetchdf()
    finally:
        connection.close()
