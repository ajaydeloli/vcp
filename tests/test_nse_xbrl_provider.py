"""Tests for the NSE XBRL fundamentals provider.

Runs entirely offline against real filings saved to tests/fixtures/nse_xbrl/
(fetched and verified live from NSE on 2026-09-27 -- see PROJECT-CONTEXT.md).
No network access in these tests: _parse_filing() is exercised directly
with real XML bodies and hand-built index-row metadata shaped like the
actual NSE API response, so this suite never depends on NSE being
reachable or unchanged.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sepa_scanner.ingestion.providers.nse_xbrl import NSEXBRLProvider

FIXTURES = Path(__file__).parent / "fixtures" / "nse_xbrl"


def _index_row(**overrides) -> dict:
    row = {
        "broadCastDate": "16-Jan-2025 20:20:21",
        "consolidated": "Non-Consolidated",
        "audited": "Un-Audited",
        "fromDate": "01-Oct-2024",
        "toDate": "31-Dec-2024",
        "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/example.xml",
    }
    row.update(overrides)
    return row


def test_parses_a_real_reliance_filing_with_plausible_values():
    provider = NSEXBRLProvider()
    xml_text = (FIXTURES / "reliance_q3fy25.xml").read_text(encoding="utf-8")

    filing = provider._parse_filing("RELIANCE", _index_row(), xml_text)

    assert filing is not None
    assert filing.symbol == "RELIANCE"
    assert filing.period_start == date(2024, 10, 1)
    assert filing.period_end == date(2024, 12, 31)
    assert filing.announcement_date == date(2025, 1, 16)
    assert filing.revenue is not None and filing.revenue > 0
    assert filing.pat is not None and filing.pat > 0
    assert filing.fields_missing is False
    assert filing.source == "nse_xbrl"


def test_parses_a_real_cdsl_filing():
    provider = NSEXBRLProvider()
    xml_text = (FIXTURES / "cdsl_q3fy25.xml").read_text(encoding="utf-8")

    filing = provider._parse_filing(
        "CDSL", _index_row(consolidated="Consolidated", broadCastDate="25-Jan-2025 14:22:20"), xml_text
    )

    assert filing is not None
    assert filing.revenue is not None and filing.revenue > 0
    assert filing.basic_eps is not None
    assert filing.fields_missing is False


def test_bank_filing_parses_but_flags_fields_missing():
    """Confirmed live (2026-09-27): banks use a different XBRL taxonomy and
    the standard revenue/PAT/EPS tags don't map. This must surface as
    fields_missing=True, not silently pass through as a zero or crash the
    batch -- a future ingestion job needs this flag to treat bank filings
    as unevaluated for now rather than scoring them incorrectly."""
    provider = NSEXBRLProvider()
    xml_text = (FIXTURES / "hdfcbank_q3fy25.xml").read_text(encoding="utf-8")

    filing = provider._parse_filing(
        "HDFCBANK", _index_row(consolidated="Consolidated", broadCastDate="23-Jan-2025 12:27:21"), xml_text
    )

    assert filing is not None
    assert filing.fields_missing is True
    assert filing.revenue is None
    assert filing.pat is None


def test_malformed_xbrl_returns_none_not_an_exception():
    """A single bad filing must not be able to crash a batch ingestion run
    over every other symbol."""
    provider = NSEXBRLProvider()

    filing = provider._parse_filing("BADSYMBOL", _index_row(), "<not valid xbrl at all>")

    assert filing is None


def test_missing_xbrl_field_falls_back_to_index_metadata_dates():
    """If nse-xbrl parses a filing but doesn't report a period, fall back to
    the filing-index metadata's own fromDate/toDate."""
    provider = NSEXBRLProvider()
    xml_text = (FIXTURES / "reliance_q3fy25.xml").read_text(encoding="utf-8")

    # Force the metadata path by patching nse-xbrl's own dates to None,
    # simulating a filing where the document body parses but the XBRL
    # period context is absent (nse-xbrl leaves absent fields as None per
    # its own documented contract).
    import sepa_scanner.ingestion.providers.nse_xbrl as nse_xbrl_module

    real_from_xbrl = nse_xbrl_module.FilingResult.from_xbrl

    def patched_from_xbrl(*args, **kwargs):
        result = real_from_xbrl(*args, **kwargs)
        object.__setattr__(result, "period_start", None)
        object.__setattr__(result, "period_end", None)
        return result

    nse_xbrl_module.FilingResult.from_xbrl = staticmethod(patched_from_xbrl)
    try:
        filing = provider._parse_filing(
            "RELIANCE", _index_row(fromDate="01-Oct-2024", toDate="31-Dec-2024"), xml_text
        )
    finally:
        nse_xbrl_module.FilingResult.from_xbrl = staticmethod(real_from_xbrl)

    assert filing is not None
    assert filing.period_start == date(2024, 10, 1)
    assert filing.period_end == date(2024, 12, 31)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.requested_params = None

    def get(self, url, headers=None, params=None, timeout=None):
        self.requested_params = params
        return _FakeResponse(self._payload)


def test_fetch_filing_index_drops_rows_missing_broadcast_date_or_xbrl_link():
    """Rows with no broadCastDate or no xbrl link can't be turned into a
    QuarterlyFiling (no announcement_date, nothing to fetch) and must be
    filtered out rather than causing a KeyError downstream."""
    payload = [
        {"broadCastDate": "16-Jan-2025 20:20:21", "xbrl": "https://example/a.xml"},
        {"broadCastDate": None, "xbrl": "https://example/b.xml"},
        {"broadCastDate": "16-Jan-2025 20:20:21", "xbrl": None},
        {"broadCastDate": "16-Jan-2025 20:20:21"},
    ]
    session = _FakeSession(payload)
    provider = NSEXBRLProvider(session=session)

    rows = provider._fetch_filing_index("RELIANCE", date(2024, 10, 1), date(2025, 1, 31))

    assert len(rows) == 1
    assert rows[0]["xbrl"] == "https://example/a.xml"
    assert session.requested_params["symbol"] == "RELIANCE"
    assert session.requested_params["from_date"] == "01-10-2024"
    assert session.requested_params["to_date"] == "31-01-2025"
