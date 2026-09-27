"""Tests for the Screener.in fundamentals provider (primary source as of
2026-09-27 -- see PROJECT-CONTEXT.md and fundamentals_base.py).

Runs entirely offline against a hand-built HTML fixture shaped like
Screener.in's real "Quarterly Results" table (column headers are "Mon
YYYY" period labels, row labels are "Sales", "Expenses", "Net Profit",
"EPS in Rs", etc., some carrying a "+" segment-expand suffix) -- confirmed
against a live fetch of https://www.screener.in/company/RELIANCE/consolidated
on 2026-09-27. Unlike tests/test_nse_xbrl_provider.py's fixtures (saved
directly from a live NSE response), this fixture is hand-built to match
that observed structure, not a captured raw page -- see screener.py's
module docstring for why a real live-fetch validation is still owed before
a full-universe run.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from config.settings import ScreenerSettings
from sepa_scanner.ingestion.providers.screener import ScreenerProvider

_CONSOLIDATED_HTML = """
<html><body>
<section id="quarters">
  <p>Consolidated Figures in Rs. Crores / <a href="#">View Standalone</a></p>
  <table>
    <thead>
      <tr><th></th><th>Jun 2023</th><th>Sep 2023</th><th>Mar 2024</th></tr>
    </thead>
    <tbody>
      <tr><td class="text">Sales <button>+</button></td><td>207,559</td><td>231,886</td><td>236,533</td></tr>
      <tr><td class="text">Expenses <button>+</button></td><td>169,466</td><td>190,918</td><td>194,017</td></tr>
      <tr><td>Operating Profit</td><td>38,093</td><td>40,968</td><td>42,516</td></tr>
      <tr><td>OPM %</td><td>18%</td><td>18%</td><td>18%</td></tr>
      <tr><td class="text">Net Profit <button>+</button></td><td>18,258</td><td>19,878</td><td>21,243</td></tr>
      <tr><td>EPS in Rs</td><td>11.83</td><td>12.85</td><td>14.01</td></tr>
    </tbody>
  </table>
</section>
</body></html>
"""

_STANDALONE_HTML = _CONSOLIDATED_HTML.replace(
    "Consolidated Figures", "Standalone Figures"
).replace("View Standalone", "View Consolidated")

_BANK_LIKE_HTML = """
<html><body>
<section id="quarters">
  <p>Consolidated Figures in Rs. Crores</p>
  <table>
    <thead><tr><th></th><th>Jun 2023</th></tr></thead>
    <tbody>
      <tr><td class="text">Sales <button>+</button></td><td>-</td></tr>
      <tr><td class="text">Net Profit <button>+</button></td><td></td></tr>
      <tr><td>EPS in Rs</td><td></td></tr>
    </tbody>
  </table>
</section>
</body></html>
"""


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400 and self.status_code != 404:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Maps exact URLs to canned responses; unmapped URLs 404."""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.requested_urls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.requested_urls.append(url)
        if url in self._responses:
            return _FakeResponse(200, self._responses[url])
        return _FakeResponse(404)


def _provider(responses: dict[str, str]) -> ScreenerProvider:
    settings = ScreenerSettings(base_url="https://example-screener.test", request_delay_seconds=0.0)
    return ScreenerProvider(settings=settings, session=_FakeSession(responses))


def test_parses_consolidated_quarterly_table_into_filings():
    provider = _provider({
        "https://example-screener.test/company/RELIANCE/consolidated/": _CONSOLIDATED_HTML,
    })

    filings = provider.fetch_quarterly("RELIANCE", date(2020, 1, 1), date(2030, 1, 1))

    assert len(filings) == 3
    by_period_end = {f.period_end: f for f in filings}
    q1 = by_period_end[date(2023, 6, 30)]
    assert q1.period_start == date(2023, 4, 1)
    assert q1.revenue == pytest.approx(207_559 * 10_000_000)
    assert q1.pat == pytest.approx(18_258 * 10_000_000)
    assert q1.basic_eps == pytest.approx(11.83)
    assert q1.diluted_eps == pytest.approx(11.83)
    assert q1.is_consolidated is True
    assert q1.source == "screener"
    assert q1.fields_missing is False
    assert q1.announcement_date_is_estimated is True


def test_estimated_announcement_date_uses_configured_lag_and_annual_lag():
    provider = _provider({
        "https://example-screener.test/company/RELIANCE/consolidated/": _CONSOLIDATED_HTML,
    })

    filings = provider.fetch_quarterly("RELIANCE", date(2020, 1, 1), date(2030, 1, 1))
    by_period_end = {f.period_end: f for f in filings}

    regular_quarter = by_period_end[date(2023, 6, 30)]
    assert regular_quarter.announcement_date == date(2023, 6, 30) + timedelta(days=45)

    annual_quarter = by_period_end[date(2024, 3, 31)]
    assert annual_quarter.announcement_date == date(2024, 3, 31) + timedelta(days=60)
    assert annual_quarter.is_audited is True
    assert regular_quarter.is_audited is False


def test_from_date_to_date_window_filters_by_estimated_announcement_date():
    provider = _provider({
        "https://example-screener.test/company/RELIANCE/consolidated/": _CONSOLIDATED_HTML,
    })

    # Mar 2024 quarter's estimated announcement_date is Mar 2024 + 60 days;
    # a window ending before that must exclude it while still returning
    # the two earlier quarters.
    filings = provider.fetch_quarterly("RELIANCE", date(2020, 1, 1), date(2024, 1, 1))

    assert {f.period_end for f in filings} == {date(2023, 6, 30), date(2023, 9, 30)}


def test_falls_back_to_standalone_when_no_consolidated_page_exists():
    provider = _provider({
        "https://example-screener.test/company/SMALLCAP/": _STANDALONE_HTML,
        # No consolidated URL registered -> 404 from the fake session.
    })

    filings = provider.fetch_quarterly("SMALLCAP", date(2020, 1, 1), date(2030, 1, 1))

    assert len(filings) == 3
    assert all(f.is_consolidated is False for f in filings)


def test_blank_cells_produce_fields_missing_true_not_zero():
    provider = _provider({
        "https://example-screener.test/company/BANKLIKE/consolidated/": _BANK_LIKE_HTML,
    })

    filings = provider.fetch_quarterly("BANKLIKE", date(2020, 1, 1), date(2030, 1, 1))

    assert len(filings) == 1
    filing = filings[0]
    assert filing.fields_missing is True
    assert filing.revenue is None
    assert filing.pat is None
    assert filing.basic_eps is None


def test_returns_empty_list_when_both_pages_404():
    provider = _provider({})

    filings = provider.fetch_quarterly("NOSUCHSYMBOL", date(2020, 1, 1), date(2030, 1, 1))

    assert filings == []
