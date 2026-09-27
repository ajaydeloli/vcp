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


class FundamentalsSettings(BaseModel):
    """Configuration for the two-layer fundamentals data architecture:
    NSE/BSE exchange XBRL filings as the primary source of truth, with
    Screener.in as a secondary, sampled cross-check. See PROJECT-CONTEXT.md
    ("Phase 3 progress") for the full reasoning.
    """

    fallback_source: Literal["none", "screener"] = "none"
    # If a symbol-quarter's NSE/BSE XBRL filing fails to fetch or parse and
    # this is "screener", ingestion fetches that one symbol-quarter from
    # Screener.in instead. The resulting fundamentals_quarterly row must
    # always be tagged source="screener_fallback" (vs. "nse_xbrl") so
    # fallback data stays distinguishable from primary data downstream --
    # it never silently masquerades as a verified exchange filing. Default
    # is "none": a missing primary data point stays unevaluated (consistent
    # with how every other analytics component already treats missing
    # data) rather than being silently filled from a source with no
    # official API. Turn this on deliberately, not as a default-on
    # convenience.

    cross_check_enabled: bool = True
    # Independent of fallback_source. Periodically re-fetches a sample of
    # symbol-quarters from Screener.in purely to compare against the
    # already-stored NSE/BSE XBRL value and flag disagreements in a
    # data-quality report -- mirrors sepa_scanner/ingestion/data_quality.py.
    # Never writes to fundamentals_quarterly.
    cross_check_sample_pct: float = 0.10


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
