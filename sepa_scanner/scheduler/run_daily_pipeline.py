"""Daily pipeline runner."""

from sepa_scanner.ingestion.daily_update import run_daily_update
from sepa_scanner.ingestion.providers import create_data_provider
from sepa_scanner.ingestion.universe import apply_active_universe, sync_provider_instruments


def main() -> None:
    """Refresh mappings, apply the configured scan scope, then ingest missing data."""
    provider = create_data_provider()
    sync_provider_instruments(provider)
    apply_active_universe(provider)
    run_daily_update(provider)


if __name__ == "__main__":
    main()
