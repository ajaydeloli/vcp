"""Application settings loaded from environment variables and a local .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class KiteSettings(BaseModel):
    """Credentials and request limits for the Kite Connect adapter."""

    api_key: str | None = None
    access_token: str | None = None
    api_secret: str | None = None
    max_historical_days: int = 1_900
    request_delay_seconds: float = 0.4
    max_retries: int = 3

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.access_token)


class DhanSettings(BaseModel):
    """Reserved configuration for the future Dhan adapter."""

    client_id: str | None = None
    access_token: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.access_token)


class UpstoxSettings(BaseModel):
    """Reserved configuration for the future Upstox adapter."""

    client_id: str | None = None
    client_secret: str | None = None
    access_token: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.access_token)


class ScreenerSettings(BaseModel):
    """Configuration for the Screener.in fundamentals provider (primary
    source as of 2026-09-27 -- see FundamentalsSettings and
    sepa_scanner/ingestion/providers/screener.py).
    """

    base_url: str = "https://www.screener.in"
    request_delay_seconds: float = 1.0
    max_retries: int = 3

    estimated_quarterly_announcement_lag_days: int = 45
    estimated_annual_announcement_lag_days: int = 60
    # Screener's quarterly results table exposes the reporting period
    # (e.g. "Mar 2024") but not the actual regulatory broadcast date, so
    # announcement_date is estimated as period_end + one of these lags
    # (SEBI LODR Regulation 33's normal disclosure windows: 45 days for a
    # regular quarter, 60 for the March/annual quarter), never a verified
    # date. Every Screener-sourced QuarterlyFiling carries
    # announcement_date_is_estimated=True so this is never confused with a
    # real broadcast date downstream.


class FundamentalsSettings(BaseModel):
    """Configuration for fundamentals data sourcing.

    Screener.in is the PRIMARY source as of 2026-09-27 (decision reversed
    from the original "NSE/BSE XBRL primary, Screener secondary" design --
    see PROJECT-CONTEXT.md). Live validation found NSE has no single
    destination that reliably returns the newest filings for a symbol: the
    windowed financial-results endpoint returned zero new rows for several
    large-caps that had, in fact, filed elsewhere by the same date.
    NSE/BSE XBRL is kept available as an optional fallback/cross-check
    source, off by default, per this project's existing "turn on
    deliberately" convention for anything not the primary source of truth.
    """

    primary_source: Literal["screener", "nse_xbrl"] = "screener"

    fallback_source: Literal["none", "nse_xbrl", "screener"] = "none"
    # If a symbol-quarter's primary-source filing fails to fetch or parse
    # and this names the other source, ingestion fetches that one
    # symbol-quarter from the fallback instead. The resulting
    # fundamentals_quarterly row must always be tagged
    # source="<fallback>_fallback" so fallback data stays distinguishable
    # from primary data downstream -- it never silently masquerades as a
    # primary-source filing. Default is "none": a missing primary data
    # point stays unevaluated (consistent with how every other analytics
    # component already treats missing data) rather than being silently
    # filled from a secondary source. Turn this on deliberately, not as a
    # default-on convenience.

    cross_check_enabled: bool = True
    # Independent of fallback_source. Periodically re-fetches a sample of
    # symbol-quarters from the non-primary source purely to compare against
    # the already-stored primary value and flag disagreements in a
    # data-quality report -- mirrors sepa_scanner/ingestion/data_quality.py.
    # Never writes to fundamentals_quarterly.
    cross_check_sample_pct: float = 0.10

    backlog_lag_threshold_days: int = 180
    # A filing is flagged is_backlog_filing when announcement_date is more
    # than this many days after period_end -- roughly two quarters past the
    # normal ~45-day post-quarter announcement cycle. Confirmed real case
    # (AHLWEST, 2026-09-27): a filing announced 5+ years after its
    # period_end. The threshold is config-driven, not hardcoded in the
    # ingestion job, per this project's "weights/thresholds live in YAML or
    # settings" rule. Note: this can only ever fire on a row with a real
    # (non-estimated) announcement_date -- see ScreenerSettings above.

    screener: ScreenerSettings = ScreenerSettings()


class Settings(BaseSettings):
    """Runtime configuration. Secrets stay in environment variables, never source control."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    data_provider: Literal["kite", "dhan", "upstox"] = "kite"
    data_directory: Path = Path("data")
    universe_scope: Literal["nifty500", "full_nse", "custom"] = "nifty500"
    nifty500_constituents_url: str = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
    custom_universe_path: Path | None = None
    kite: KiteSettings = KiteSettings()
    dhan: DhanSettings = DhanSettings()
    upstox: UpstoxSettings = UpstoxSettings()
    fundamentals: FundamentalsSettings = FundamentalsSettings()

    @property
    def provider_configured(self) -> bool:
        """Return whether the selected provider has the credentials it requires."""
        return getattr(self, self.data_provider).configured


@lru_cache
def get_settings() -> Settings:
    """Return one settings instance per process."""
    return Settings()
