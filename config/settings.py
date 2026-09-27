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

    @property
    def provider_configured(self) -> bool:
        """Return whether the selected provider has the credentials it requires."""
        return getattr(self, self.data_provider).configured


@lru_cache
def get_settings() -> Settings:
    """Return one settings instance per process."""
    return Settings()
