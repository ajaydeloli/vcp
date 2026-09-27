"""FastAPI application entry point."""

from fastapi import FastAPI

from config.settings import get_settings

app = FastAPI(title="SEPA Scanner API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the API process is ready."""
    return {"status": "ok"}


@app.get("/settings/provider")
def get_provider_settings() -> dict[str, object]:
    """Report the selected source without exposing its credentials."""
    settings = get_settings()
    return {
        "provider": settings.data_provider,
        "configured": settings.provider_configured
        if settings.data_provider == "kite"
        else False,
    }


@app.get("/universe")
def get_universe() -> list[dict[str, str]]:
    """Return the tracked symbol universe (empty until configured)."""
    return []


@app.get("/scores/today")
def get_today_scores() -> list[dict[str, object]]:
    """Return today's ranked candidates (empty until the pipeline is implemented)."""
    return []
