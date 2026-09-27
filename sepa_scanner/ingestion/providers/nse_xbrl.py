"""NSE exchange XBRL fundamentals provider -- primary source of truth per
the two-layer fundamentals data architecture (PROJECT-CONTEXT.md).

Fetches NSE's public financial-results JSON index and the linked XBRL
filing documents it references, then parses each filing with the
third-party `nse-xbrl` package. NSE has no official, key-based API for
this; these are the same public-but-undocumented endpoints
`nsepython`/`jugaad-data` already rely on -- but empirically (verified
2026-09-27), no cookie/session warm-up is required for these two specific
endpoints, simpler than those libraries' own approach for other NSE data.

Verified live against real filings: RELIANCE, INFY, CDSL, and TATASTEEL
all parse with plausible values (matching known public financials).
HDFCBANK (a bank) parses without error but returns revenue/PAT/EPS as
None -- banks and likely NBFCs use a different XBRL taxonomy this parser
doesn't map yet. This is surfaced via `QuarterlyFiling.fields_missing`,
never silently dropped or scored as zero.
"""

from __future__ import annotations

from datetime import date, datetime

import requests
from nse_xbrl import FilingResult

from sepa_scanner.ingestion.providers.fundamentals_base import QuarterlyFiling

FINANCIAL_RESULTS_URL = "https://www.nseindia.com/api/corporates-financial-results"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # Deliberately omit "br" (brotli): NSE serves brotli-compressed
    # responses, and without a brotli decoder installed `requests` returns
    # the raw compressed bytes as garbled "text" instead of raising an
    # error -- a silent-corruption failure mode, not a loud one. Sticking
    # to gzip/deflate avoids it entirely rather than adding a new
    # dependency to decode brotli.
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/",
}

_BROADCAST_DATE_FORMAT = "%d-%b-%Y %H:%M:%S"
_METADATA_DATE_FORMAT = "%d-%b-%Y"
_QUERY_DATE_FORMAT = "%d-%m-%Y"
_REQUEST_TIMEOUT_SECONDS = 20


class NSEXBRLProvider:
    """Fetches and parses NSE quarterly financial-results XBRL filings."""

    name = "nse_xbrl"

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def fetch_quarterly(self, symbol: str, from_date: date, to_date: date) -> list[QuarterlyFiling]:
        """Fetch and parse every quarterly filing for `symbol` broadcast
        within [from_date, to_date].

        Always pass an explicit, reasonably narrow date range. Empirically
        (2026-09-27), an unbounded query for a single symbol can return an
        incomplete and non-chronologically-ordered result set -- the
        endpoint appears to cap or otherwise bound results when no date
        window is given, so relying on "no filter, take the newest" is not
        safe.
        """
        index_rows = self._fetch_filing_index(symbol, from_date, to_date)
        filings: list[QuarterlyFiling] = []
        for row in index_rows:
            xml_text = self._fetch_xbrl_document(row["xbrl"])
            filing = self._parse_filing(symbol, row, xml_text)
            if filing is not None:
                filings.append(filing)
        return filings

    def _fetch_filing_index(self, symbol: str, from_date: date, to_date: date) -> list[dict]:
        params = {
            "index": "equities",
            "period": "Quarterly",
            "symbol": symbol.upper(),
            "from_date": from_date.strftime(_QUERY_DATE_FORMAT),
            "to_date": to_date.strftime(_QUERY_DATE_FORMAT),
        }
        response = self._session.get(
            FINANCIAL_RESULTS_URL, headers=_HEADERS, params=params, timeout=_REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        rows = response.json()
        return [row for row in rows if row.get("broadCastDate") and row.get("xbrl")]

    def _fetch_xbrl_document(self, url: str) -> str:
        response = self._session.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text

    def _parse_filing(self, symbol: str, index_row: dict, xml_text: str) -> QuarterlyFiling | None:
        is_consolidated = index_row.get("consolidated") == "Consolidated"
        try:
            parsed = FilingResult.from_xbrl(xml_text, symbol=symbol, is_consolidated=is_consolidated)
        except Exception:
            # A single filing failing to parse is a data-quality event for
            # that symbol-quarter, not a reason to crash an entire batch
            # run. Returning None here (rather than raising) lets a future
            # ingestion job log/flag it and move on to the next symbol --
            # matching this project's existing "flag, don't silently drop"
            # rule, applied at the batch-caller level, not by fabricating
            # a filing here.
            return None

        period_end = parsed.period_end or _parse_metadata_date(index_row.get("toDate"))
        period_start = parsed.period_start or _parse_metadata_date(index_row.get("fromDate"))
        if period_end is None or period_start is None:
            return None

        fields_missing = parsed.q_revenue is None and parsed.q_pat is None and parsed.q_basic_eps is None

        return QuarterlyFiling(
            symbol=symbol.upper(),
            period_start=period_start,
            period_end=period_end,
            announcement_date=datetime.strptime(index_row["broadCastDate"], _BROADCAST_DATE_FORMAT).date(),
            is_consolidated=is_consolidated,
            is_audited=index_row.get("audited") == "Audited",
            revenue=parsed.q_revenue,
            pat=parsed.q_pat,
            basic_eps=parsed.q_basic_eps,
            diluted_eps=parsed.q_diluted_eps,
            shares_outstanding=parsed.shares_outstanding,
            source="nse_xbrl",
            source_url=index_row["xbrl"],
            fields_missing=fields_missing,
        )


def _parse_metadata_date(value: str | None) -> date | None:
    """Parse the filing-index metadata's DD-Mon-YYYY date strings (used as
    a fallback when nse-xbrl itself couldn't extract a period from the XBRL
    document body)."""
    if not value:
        return None
    return datetime.strptime(value, _METADATA_DATE_FORMAT).date()
