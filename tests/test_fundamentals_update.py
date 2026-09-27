"""Tests for the fundamentals ingestion job (sepa_scanner/ingestion/fundamentals_update.py).

Uses a fake FundamentalsProvider (no network) and a temporary DuckDB file,
mirroring tests/test_volume_signals_persistence.py's pattern. Three real,
confirmed scenarios from PROJECT-CONTEXT.md are each covered directly:
  - normal YoY growth via the ~4-quarters-prior self-join,
  - a fields_missing=True symbol (banks/NBFCs) whose growth must be NULL,
    never 0, on both sides of the comparison,
  - a backlog filing (announcement_date far past period_end) that is still
    stored but flagged, not silently dropped or silently treated as current.
"""

from __future__ import annotations

from datetime import date

import pytest

from config.settings import Settings
from sepa_scanner.ingestion.providers.fundamentals_base import QuarterlyFiling
from sepa_scanner.ingestion.fundamentals_update import latest_fundamentals, run_fundamentals_update
from sepa_scanner.storage.db import connect_analytics, initialize_analytics


def _filing(
    symbol: str,
    period_start: date,
    period_end: date,
    announcement_date: date,
    *,
    eps: float | None = None,
    revenue: float | None = None,
    fields_missing: bool = False,
) -> QuarterlyFiling:
    return QuarterlyFiling(
        symbol=symbol,
        period_start=period_start,
        period_end=period_end,
        announcement_date=announcement_date,
        is_consolidated=True,
        is_audited=True,
        revenue=revenue,
        pat=None,
        basic_eps=eps,
        diluted_eps=eps,
        shares_outstanding=None,
        source="nse_xbrl",
        source_url="https://nsearchives.nseindia.com/corporate/xbrl/example.xml",
        fields_missing=fields_missing,
    )


class _FakeFundamentalsProvider:
    """No-network stand-in for NSEXBRLProvider. Filters exactly like the real
    provider's endpoint does: only filings whose announcement_date falls in
    the requested [from_date, to_date] window are returned."""

    name = "fake_fundamentals"

    def __init__(self, filings_by_symbol: dict[str, list[QuarterlyFiling]]) -> None:
        self._filings_by_symbol = filings_by_symbol

    def fetch_quarterly(self, symbol: str, from_date: date, to_date: date) -> list[QuarterlyFiling]:
        return [
            filing
            for filing in self._filings_by_symbol.get(symbol, [])
            if from_date <= filing.announcement_date <= to_date
        ]


@pytest.fixture()
def universe_settings(tmp_path):
    settings = Settings(data_directory=tmp_path)
    connection = initialize_analytics(tmp_path / "market.duckdb")
    try:
        connection.execute(
            """INSERT INTO universe (symbol, is_active) VALUES
               ('FUNDTEST', TRUE), ('BANKTEST', TRUE), ('BACKLOGTEST', TRUE)"""
        )
    finally:
        connection.close()
    return settings


def _fetch_symbol_rows(settings: Settings, symbol: str):
    connection = connect_analytics(settings.data_directory / "market.duckdb")
    try:
        return connection.execute(
            "SELECT * FROM fundamentals_quarterly WHERE symbol = ? ORDER BY quarter_end", [symbol]
        ).fetchdf()
    finally:
        connection.close()


def test_computes_yoy_growth_via_self_join_against_prior_year_quarter(universe_settings):
    provider = _FakeFundamentalsProvider({
        "FUNDTEST": [
            _filing(
                "FUNDTEST", date(2023, 4, 1), date(2023, 6, 30), date(2023, 7, 20),
                eps=10.0, revenue=1000.0,
            ),
            _filing(
                "FUNDTEST", date(2024, 4, 1), date(2024, 6, 30), date(2024, 7, 18),
                eps=12.0, revenue=1200.0,
            ),
        ]
    })

    summary = run_fundamentals_update(provider, settings=universe_settings, end_date=date(2024, 8, 1))

    assert summary["symbols_failed"] == 0
    assert summary["rows_added"] == 2
    rows = _fetch_symbol_rows(universe_settings, "FUNDTEST")
    assert len(rows) == 2
    first, second = rows.iloc[0], rows.iloc[1]
    assert first["eps_yoy_growth"] is None or pd_isna(first["eps_yoy_growth"])
    assert second["eps_yoy_growth"] == pytest.approx(20.0)
    assert second["sales_yoy_growth"] == pytest.approx(20.0)
    assert second["source"] == "nse_xbrl"
    assert bool(second["fields_missing"]) is False
    assert bool(second["is_backlog_filing"]) is False
    # QuarterlyFiling.announcement_date_is_estimated defaults to False for
    # providers (like this fake one, modeled on NSEXBRLProvider) that report
    # a real broadcast date -- see fundamentals_base.py's module docstring.
    assert bool(second["announcement_date_is_estimated"]) is False


def test_fields_missing_row_stores_null_growth_not_zero(universe_settings):
    provider = _FakeFundamentalsProvider({
        "BANKTEST": [
            _filing(
                "BANKTEST", date(2023, 4, 1), date(2023, 6, 30), date(2023, 7, 20),
                fields_missing=True,
            ),
            _filing(
                "BANKTEST", date(2024, 4, 1), date(2024, 6, 30), date(2024, 7, 18),
                fields_missing=True,
            ),
        ]
    })

    run_fundamentals_update(provider, settings=universe_settings, end_date=date(2024, 8, 1))

    rows = _fetch_symbol_rows(universe_settings, "BANKTEST")
    assert len(rows) == 2
    for _, row in rows.iterrows():
        assert bool(row["fields_missing"]) is True
        assert row["eps"] is None or pd_isna(row["eps"])
        assert row["eps_yoy_growth"] is None or pd_isna(row["eps_yoy_growth"])
        assert row["sales_yoy_growth"] is None or pd_isna(row["sales_yoy_growth"])


def test_backlog_filing_is_stored_and_flagged_not_dropped(universe_settings):
    """Mirrors the real, confirmed AHLWEST case: a filing announced more than
    five years after its period_end must still be persisted, with the gap
    surfaced explicitly rather than silently treated as current."""
    provider = _FakeFundamentalsProvider({
        "BACKLOGTEST": [
            _filing(
                "BACKLOGTEST", date(2021, 1, 1), date(2021, 3, 31), date(2026, 8, 24),
                eps=5.0, revenue=500.0,
            ),
        ]
    })

    summary = run_fundamentals_update(provider, settings=universe_settings, end_date=date(2026, 9, 1))

    assert summary["rows_added"] == 1
    rows = _fetch_symbol_rows(universe_settings, "BACKLOGTEST")
    assert len(rows) == 1
    row = rows.iloc[0]
    assert bool(row["is_backlog_filing"]) is True
    assert row["announcement_lag_days"] == (date(2026, 8, 24) - date(2021, 3, 31)).days
    assert row["eps"] == pytest.approx(5.0)


def test_consolidated_filing_is_preferred_over_standalone_for_the_same_quarter(universe_settings):
    """Real, confirmed case (live validation run, 2026-09-27): NSE's filing
    index returns BOTH a consolidated and a standalone filing for the same
    quarter (RELIANCE FY25 Q2: consolidated EPS 24.48 vs standalone 11.40).
    Both would map to the same (symbol, quarter_end) primary key, so
    whichever the loop processed last would silently overwrite the other.
    Consolidated must win regardless of fetch order.
    """
    standalone_first = _FakeFundamentalsProvider({
        "FUNDTEST": [
            QuarterlyFiling(
                symbol="FUNDTEST", period_start=date(2024, 4, 1), period_end=date(2024, 6, 30),
                announcement_date=date(2024, 7, 19), is_consolidated=False, is_audited=True,
                revenue=100.0, pat=None, basic_eps=11.4, diluted_eps=11.4, shares_outstanding=None,
                source="nse_xbrl", source_url="https://example/standalone.xml", fields_missing=False,
            ),
            QuarterlyFiling(
                symbol="FUNDTEST", period_start=date(2024, 4, 1), period_end=date(2024, 6, 30),
                announcement_date=date(2024, 7, 19), is_consolidated=True, is_audited=True,
                revenue=235.0, pat=None, basic_eps=24.48, diluted_eps=24.48, shares_outstanding=None,
                source="nse_xbrl", source_url="https://example/consolidated.xml", fields_missing=False,
            ),
        ]
    })

    run_fundamentals_update(standalone_first, settings=universe_settings, end_date=date(2024, 8, 1))

    rows = _fetch_symbol_rows(universe_settings, "FUNDTEST")
    assert len(rows) == 1
    assert rows.iloc[0]["eps"] == pytest.approx(24.48)
    assert rows.iloc[0]["sales"] == pytest.approx(235.0)


def test_rerun_is_idempotent(universe_settings):
    provider = _FakeFundamentalsProvider({
        "FUNDTEST": [
            _filing(
                "FUNDTEST", date(2023, 4, 1), date(2023, 6, 30), date(2023, 7, 20),
                eps=10.0, revenue=1000.0,
            ),
            _filing(
                "FUNDTEST", date(2024, 4, 1), date(2024, 6, 30), date(2024, 7, 18),
                eps=12.0, revenue=1200.0,
            ),
        ]
    })

    first = run_fundamentals_update(provider, settings=universe_settings, end_date=date(2024, 8, 1))
    second = run_fundamentals_update(provider, settings=universe_settings, end_date=date(2024, 8, 1))

    assert first["rows_added"] == 2
    assert second["rows_added"] == 0
    rows = _fetch_symbol_rows(universe_settings, "FUNDTEST")
    assert len(rows) == 2
    assert rows.iloc[1]["eps_yoy_growth"] == pytest.approx(20.0)


def test_latest_fundamentals_returns_the_most_recent_quarter_per_symbol(universe_settings):
    provider = _FakeFundamentalsProvider({
        "FUNDTEST": [
            _filing(
                "FUNDTEST", date(2023, 4, 1), date(2023, 6, 30), date(2023, 7, 20),
                eps=10.0, revenue=1000.0,
            ),
            _filing(
                "FUNDTEST", date(2024, 4, 1), date(2024, 6, 30), date(2024, 7, 18),
                eps=12.0, revenue=1200.0,
            ),
        ]
    })
    run_fundamentals_update(provider, settings=universe_settings, end_date=date(2024, 8, 1))

    latest = latest_fundamentals(settings=universe_settings)

    fundtest_rows = latest[latest["symbol"] == "FUNDTEST"]
    assert len(fundtest_rows) == 1
    assert fundtest_rows.iloc[0]["quarter_end"].date() == date(2024, 6, 30)


def pd_isna(value) -> bool:
    import pandas as pd

    return bool(pd.isna(value))
