"""Tests for FundamentalsSettings -- specifically that the Screener.in
fallback defaults to off. This default is a deliberate data-integrity
decision (see PROJECT-CONTEXT.md), not an arbitrary default, so it gets
its own regression test rather than relying on incidental coverage.
"""

from __future__ import annotations

from config.settings import Settings


def test_fundamentals_fallback_defaults_to_none():
    settings = Settings()

    assert settings.fundamentals.fallback_source == "none"


def test_fundamentals_fallback_can_be_enabled_via_env(monkeypatch):
    monkeypatch.setenv("FUNDAMENTALS__FALLBACK_SOURCE", "screener")

    settings = Settings()

    assert settings.fundamentals.fallback_source == "screener"


def test_fundamentals_cross_check_defaults_on_and_sampled():
    settings = Settings()

    assert settings.fundamentals.cross_check_enabled is True
    assert 0.0 < settings.fundamentals.cross_check_sample_pct < 1.0
