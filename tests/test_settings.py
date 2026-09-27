"""Tests for FundamentalsSettings -- specifically that Screener.in is the
default primary source (decided 2026-09-27, see PROJECT-CONTEXT.md) and
that the fallback to the non-primary source defaults to off. These are
deliberate data-integrity decisions, not arbitrary defaults, so they get
their own regression tests rather than relying on incidental coverage.
"""

from __future__ import annotations

from config.settings import Settings


def test_fundamentals_primary_source_defaults_to_screener():
    settings = Settings()

    assert settings.fundamentals.primary_source == "screener"


def test_fundamentals_primary_source_can_be_switched_via_env(monkeypatch):
    monkeypatch.setenv("FUNDAMENTALS__PRIMARY_SOURCE", "nse_xbrl")

    settings = Settings()

    assert settings.fundamentals.primary_source == "nse_xbrl"


def test_fundamentals_fallback_defaults_to_none():
    settings = Settings()

    assert settings.fundamentals.fallback_source == "none"


def test_fundamentals_fallback_can_be_enabled_via_env(monkeypatch):
    monkeypatch.setenv("FUNDAMENTALS__FALLBACK_SOURCE", "nse_xbrl")

    settings = Settings()

    assert settings.fundamentals.fallback_source == "nse_xbrl"


def test_fundamentals_cross_check_defaults_on_and_sampled():
    settings = Settings()

    assert settings.fundamentals.cross_check_enabled is True
    assert 0.0 < settings.fundamentals.cross_check_sample_pct < 1.0


def test_screener_settings_have_sane_defaults():
    settings = Settings()

    assert settings.fundamentals.screener.base_url == "https://www.screener.in"
    assert settings.fundamentals.screener.estimated_quarterly_announcement_lag_days == 45
    assert settings.fundamentals.screener.estimated_annual_announcement_lag_days == 60
