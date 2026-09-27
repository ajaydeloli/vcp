"""Screener.in fundamentals provider -- PRIMARY source of truth as of
2026-09-27 (see sepa_scanner/ingestion/providers/fundamentals_base.py's
module docstring and PROJECT-CONTEXT.md for the full reasoning).

Scrapes the "Quarterly Results" table on a company's Screener.in page
(consolidated preferred, standalone fallback -- mirroring the
consolidated-preferred rule already used for NSE filings in
sepa_scanner/ingestion/fundamentals_update.py's _select_preferred_filings).
Screener has no official API; this targets the public company page HTML
directly, the same way NSEXBRLProvider targets NSE's public-but-undocumented
JSON endpoints.

IMPORTANT LIMITATION, not hypothetical: Screener's quarterly table exposes
only the reporting period (e.g. "Mar 2024"), not the actual regulatory
announcement/broadcast date. `announcement_date` on every filing this
provider returns is therefore an ESTIMATE (period_end + a configured
typical SEBI disclosure-window lag), and `announcement_date_is_estimated`
is always True. See ScreenerSettings in config/settings.py for the lag
defaults and QuarterlyFiling's docstring for why this matters for
point-in-time-correct scoring.

VALIDATION STATUS: unlike nse_xbrl.py (verified 2026-09-27 against live
filings with a real, captured HTML fixture -- see tests/fixtures/nse_xbrl/),
this parser has NOT yet been run against a live-fetched Screener.in page.
Its target table structure (a `#quarters` section containing a `<table>`
with quarter-label column headers and row-label rows such as "Sales",
"Net Profit", "EPS in Rs") is based on Screener's publicly documented page
layout, and the parser is written to key off row-label text rather than
brittle CSS classes so it tolerates markup/styling changes -- but per this
project's own rule ("before a full external-data job, run a small
controlled validation first"), run a real fetch_quarterly() call against a
few known symbols (e.g. RELIANCE, INFY, HDFCBANK) and compare against the
live Screener.in page before trusting this in a full-universe run.
"""

from __future__ import annotations

import calendar
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from config.settings import ScreenerSettings
from sepa_scanner.ingestion.providers.fundamentals_base import QuarterlyFiling

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_REQUEST_TIMEOUT_SECONDS = 20
_CRORES_TO_RUPEES = 10_000_000
# Screener reports Sales/Net Profit in Rs. Crores; NSEXBRLProvider (and this
# project's fundamentals_quarterly.sales column) use absolute rupees. This
# conversion keeps both providers' output in the same unit -- without it, a
# symbol whose data crosses a source boundary (e.g. primary fails one
# quarter and falls back to the other source) would silently corrupt its
# own YoY growth self-join with a ~10,000,000x unit mismatch.

_QUARTER_HEADER_FORMAT = "%b %Y"


class ScreenerProvider:
    """Fetches and parses Screener.in's Quarterly Results table."""

    name = "screener"

    def __init__(self, settings: ScreenerSettings | None = None, session: requests.Session | None = None) -> None:
        self._settings = settings or ScreenerSettings()
        self._session = session or requests.Session()

    def fetch_quarterly(self, symbol: str, from_date: date, to_date: date) -> list[QuarterlyFiling]:
        """Fetch every quarter Screener currently shows for `symbol` whose
        ESTIMATED announcement_date falls within [from_date, to_date].

        Screener's page is not date-windowed server-side (unlike NSE's
        endpoint) -- it always shows a trailing window of recent quarters.
        This method fetches that one page and filters client-side.
        """
        html, source_url, is_consolidated = self._fetch_company_page(symbol)
        if html is None:
            return []
        quarters = self._parse_quarterly_table(html)
        filings: list[QuarterlyFiling] = []
        for period_end, values in quarters.items():
            filing = self._build_filing(symbol, period_end, values, source_url, is_consolidated)
            if filing is not None and from_date <= filing.announcement_date <= to_date:
                filings.append(filing)
        return filings

    def _fetch_company_page(self, symbol: str) -> tuple[str | None, str, bool]:
        """Try the consolidated page first (mirrors this project's existing
        consolidated-preferred rule); fall back to the standalone page when
        no consolidated view exists for this company. Returns (html,
        source_url, is_consolidated); html is None if both attempts fail.
        """
        base = self._settings.base_url.rstrip("/")
        consolidated_url = f"{base}/company/{symbol.upper()}/consolidated/"
        standalone_url = f"{base}/company/{symbol.upper()}/"

        html = self._get_with_retries(consolidated_url)
        if html is not None and self._has_quarterly_table(html):
            # Screener labels the section "Consolidated" unless the company
            # has no consolidated financials, in which case it silently
            # serves standalone data even at the /consolidated/ URL --
            # detect which one we actually got rather than trusting the URL.
            return html, consolidated_url, "consolidated" in self._quarters_section_label(html).lower()

        html = self._get_with_retries(standalone_url)
        if html is not None and self._has_quarterly_table(html):
            return html, standalone_url, False

        return None, standalone_url, False

    def _get_with_retries(self, url: str) -> str | None:
        for attempt in range(self._settings.max_retries):
            if attempt > 0:
                time.sleep(self._settings.request_delay_seconds)
            try:
                response = self._session.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT_SECONDS)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                time.sleep(self._settings.request_delay_seconds)
                return response.text
            except requests.RequestException:
                continue
        return None

    @staticmethod
    def _has_quarterly_table(html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        section = soup.find(id="quarters")
        return section is not None and section.find("table") is not None

    @staticmethod
    def _quarters_section_label(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        section = soup.find(id="quarters")
        if section is None:
            return ""
        return section.get_text(" ", strip=True)[:200]

    def _parse_quarterly_table(self, html: str) -> dict[date, dict[str, str | None]]:
        """Return {period_end_date: {"sales": ..., "net_profit": ...,
        "eps": ...}} for every quarter column in the table, keyed by the
        parsed period-end date of that column."""
        soup = BeautifulSoup(html, "html.parser")
        section = soup.find(id="quarters")
        if section is None:
            return {}
        table = section.find("table")
        if table is None:
            return {}

        header_cells = table.find("thead").find_all("th") if table.find("thead") else []
        period_ends: list[date | None] = []
        for cell in header_cells[1:]:
            period_ends.append(_parse_quarter_header(cell.get_text(strip=True)))

        row_values: dict[str, list[str | None]] = {}
        body = table.find("tbody")
        if body is None:
            return {}
        for row in body.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            label = _normalize_row_label(cells[0].get_text(" ", strip=True))
            row_values[label] = [_cell_text(cell) for cell in cells[1:]]

        quarters: dict[date, dict[str, str | None]] = {}
        for index, period_end in enumerate(period_ends):
            if period_end is None:
                continue
            quarters[period_end] = {
                "sales": _value_at(row_values.get("sales"), index),
                "net_profit": _value_at(row_values.get("net profit"), index),
                "eps": _value_at(row_values.get("eps in rs"), index),
            }
        return quarters

    def _build_filing(
        self,
        symbol: str,
        period_end: date,
        values: dict[str, str | None],
        source_url: str,
        is_consolidated: bool,
    ) -> QuarterlyFiling | None:
        sales_crores = _parse_number(values.get("sales"))
        net_profit_crores = _parse_number(values.get("net_profit"))
        basic_eps = _parse_number(values.get("eps"))

        revenue = sales_crores * _CRORES_TO_RUPEES if sales_crores is not None else None
        pat = net_profit_crores * _CRORES_TO_RUPEES if net_profit_crores is not None else None
        fields_missing = revenue is None and pat is None and basic_eps is None

        period_start = _quarter_start(period_end)
        is_annual_quarter = period_end.month == 3
        lag_days = (
            self._settings.estimated_annual_announcement_lag_days
            if is_annual_quarter
            else self._settings.estimated_quarterly_announcement_lag_days
        )
        announcement_date = period_end + timedelta(days=lag_days)

        return QuarterlyFiling(
            symbol=symbol.upper(),
            period_start=period_start,
            period_end=period_end,
            announcement_date=announcement_date,
            is_consolidated=is_consolidated,
            # Screener's quarterly table doesn't distinguish audited vs
            # unaudited per quarter; the March/annual quarter is audited
            # under SEBI LODR, interim quarters normally aren't. This is an
            # inference from the reporting calendar, not a value Screener
            # states -- treat it as a reasonable default, not a fact.
            is_audited=is_annual_quarter,
            revenue=revenue,
            pat=pat,
            basic_eps=basic_eps,
            # Screener's quarterly table doesn't report diluted EPS
            # separately; basic and diluted are treated as equal here.
            diluted_eps=basic_eps,
            shares_outstanding=None,
            source="screener",
            source_url=source_url,
            fields_missing=fields_missing,
            announcement_date_is_estimated=True,
        )


def _parse_quarter_header(text: str) -> date | None:
    """Parse a column header like "Mar 2024" into that month's last
    calendar date (the quarter's period_end). Returns None for headers this
    table shows that aren't a quarter column (e.g. a stray blank th)."""
    try:
        parsed = datetime.strptime(text.strip(), _QUARTER_HEADER_FORMAT)
    except ValueError:
        return None
    last_day = calendar.monthrange(parsed.year, parsed.month)[1]
    return date(parsed.year, parsed.month, last_day)


def _quarter_start(period_end: date) -> date:
    """First day of the month two months before period_end's month -- a
    quarter ending in March starts on January 1, etc."""
    month = period_end.month - 2
    year = period_end.year
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _normalize_row_label(text: str) -> str:
    """Screener row labels carry a trailing "+" expand toggle for rows with
    a segment breakdown (e.g. "Sales +"); strip it and lowercase for
    label-based (not CSS-class-based) row matching."""
    return text.rstrip("+").strip().lower()


def _cell_text(cell) -> str | None:
    text = cell.get_text(strip=True)
    return text if text else None


def _value_at(values: list[str | None] | None, index: int) -> str | None:
    if values is None or index >= len(values):
        return None
    return values[index]


def _parse_number(text: str | None) -> float | None:
    if text is None:
        return None
    cleaned = text.replace(",", "").replace("%", "").strip()
    if cleaned in ("", "-"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None
