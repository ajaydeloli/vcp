"""Configured provider selection."""

from config.settings import Settings, get_settings
from sepa_scanner.ingestion.providers.base import DataProvider
from sepa_scanner.ingestion.providers.kite import KiteDataProvider


def create_data_provider(settings: Settings | None = None) -> DataProvider:
    """Create the provider selected by settings."""
    selected_settings = settings or get_settings()
    if selected_settings.data_provider == "kite":
        return KiteDataProvider(selected_settings)
    raise NotImplementedError(f"Provider '{selected_settings.data_provider}' is not implemented yet")
