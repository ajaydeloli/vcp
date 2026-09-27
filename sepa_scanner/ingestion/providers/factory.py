"""Configured provider selection."""

from config.settings import Settings, get_settings
from sepa_scanner.ingestion.providers.base import DataProvider
from sepa_scanner.ingestion.providers.fundamentals_base import FundamentalsProvider
from sepa_scanner.ingestion.providers.kite import KiteDataProvider
from sepa_scanner.ingestion.providers.nse_xbrl import NSEXBRLProvider
from sepa_scanner.ingestion.providers.screener import ScreenerProvider


def create_data_provider(settings: Settings | None = None) -> DataProvider:
    """Create the provider selected by settings."""
    selected_settings = settings or get_settings()
    if selected_settings.data_provider == "kite":
        return KiteDataProvider(selected_settings)
    raise NotImplementedError(f"Provider '{selected_settings.data_provider}' is not implemented yet")


def create_fundamentals_provider(settings: Settings | None = None) -> FundamentalsProvider:
    """Create the fundamentals provider selected by settings.fundamentals.primary_source.

    Screener.in is the default primary source as of 2026-09-27 (see
    FundamentalsSettings in config/settings.py and
    sepa_scanner/ingestion/providers/fundamentals_base.py's module
    docstring for the reasoning). NSE/BSE XBRL remains selectable, e.g. for
    the optional fallback/cross-check path, via
    create_fundamentals_provider_by_name().
    """
    selected_settings = settings or get_settings()
    return create_fundamentals_provider_by_name(selected_settings.fundamentals.primary_source, selected_settings)


def create_fundamentals_provider_by_name(name: str, settings: Settings | None = None) -> FundamentalsProvider:
    """Create a specific named fundamentals provider, independent of which
    one is configured as primary -- used for the fallback/cross-check path
    (settings.fundamentals.fallback_source), which by construction must be
    able to instantiate whichever source ISN'T primary.
    """
    selected_settings = settings or get_settings()
    if name == "screener":
        return ScreenerProvider(selected_settings.fundamentals.screener)
    if name == "nse_xbrl":
        return NSEXBRLProvider()
    raise NotImplementedError(f"Fundamentals source '{name}' is not implemented yet")
