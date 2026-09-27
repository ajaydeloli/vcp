"""Provider-neutral fundamentals data contracts.

Mirrors sepa_scanner/ingestion/providers/base.py's DataProvider/DailyBar
pattern for the fundamentals side: a normalized record type plus a
Protocol every fundamentals source implements identically.

As of 2026-09-27, Screener.in is the PRIMARY fundamentals source
(ScreenerProvider); NSE/BSE XBRL (NSEXBRLProvider) is kept as an optional
secondary/cross-check source, off by default -- reversed from the original
"NSE primary, Screener fallback" design. Reason (see PROJECT-CONTEXT.md):
live validation found there is no single NSE destination that reliably
returns the newest filings for a symbol -- the windowed financial-results
endpoint returned zero rows for several large-caps despite those symbols
having filed elsewhere by the same date. Screener.in aggregates a
pre-normalized trailing quarterly series per company (consolidated and
standalone), sidestepping that gap and also sidestepping the bank/NBFC
XBRL-taxonomy field-mapping gap NSEXBRLProvider hit for HDFCBANK.

The trade-off: Screener's quarterly results page does not expose the
regulatory announcement/broadcast date, only the reporting period. A
Screener-sourced QuarterlyFiling's `announcement_date` is therefore an
ESTIMATE (period_end + a configured typical reporting lag), flagged via
`announcement_date_is_estimated=True` -- never a verified broadcast date
the way NSEXBRLProvider's is. A consumer that needs a real, non-estimated
broadcast date (e.g. to reliably detect a genuine backlog filing, as with
the AHLWEST case below) should not rely on Screener-sourced rows for that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class QuarterlyFiling:
    """A normalized quarterly fundamentals record returned by any fundamentals provider.

    `announcement_date` (when the filing became public) and `period_end`
    (the quarter it reports on) are tracked separately and must never be
    conflated for scoring: a filing can be announced long after its
    period_end (an observed real case, not hypothetical -- a small-cap
    filed a quarterly result in August 2026 for the January-March 2021
    period, a multi-year-overdue backlog filing). Point-in-time-correct
    scoring must gate on `announcement_date`, never `period_end`.
    """

    symbol: str
    period_start: date
    period_end: date
    announcement_date: date
    is_consolidated: bool
    is_audited: bool
    revenue: float | None
    pat: float | None
    basic_eps: float | None
    diluted_eps: float | None
    shares_outstanding: float | None
    source: str
    source_url: str
    fields_missing: bool
    # True when revenue/PAT/EPS all came back None despite the filing
    # itself parsing without error -- observed live for banks (HDFCBANK):
    # the standard revenue/PAT tags don't exist in the banking XBRL
    # taxonomy (interest income/NII instead). This is a real, confirmed
    # gap (see PROJECT-CONTEXT.md), not a hypothetical one. A future
    # ingestion job must treat this as "unevaluated for this filer's
    # sector," not silently score it as zero growth.
    announcement_date_is_estimated: bool = False
    # True for Screener-sourced filings: Screener's quarterly page doesn't
    # expose a real broadcast date, so `announcement_date` is period_end +
    # a configured typical reporting lag, not a verified date. False (the
    # default) for providers -- like NSEXBRLProvider -- that report a real
    # regulatory broadcast date. See this module's docstring.


class FundamentalsProvider(Protocol):
    """Contract implemented by every fundamentals source."""

    name: str

    def fetch_quarterly(self, symbol: str, from_date: date, to_date: date) -> Sequence[QuarterlyFiling]:
        """Fetch normalized quarterly filings for one symbol, broadcast
        within the inclusive [from_date, to_date] window. Callers should
        always pass an explicit window; an unbounded query is not
        guaranteed complete or chronologically sorted by every provider."""
