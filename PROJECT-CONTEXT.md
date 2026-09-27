# SEPA Scanner — Project Context and Continuation Guide

Read this file together with `PROJECT-DESIGN.md` before changing the project. It is the authoritative handoff for future Codex or AI sessions.

## Purpose

SEPA Scanner is a single-user, local-first NSE research and screening tool. It stores daily OHLCV locally, derives weekly bars, finds Minervini Trend Template and VCP setups, calculates relative strength, then ranks qualifying stocks with an explainable 0–100 composite score.

This is a research and screening application. Do not add order placement, paper trading, multi-user behavior, microservices, Redis, queues, plugins, or LLM chat features during v1.

## Agreed architecture

- Python 3.12 application package: `sepa_scanner/`
- DuckDB: analytical market data, weekly bars, universe mappings, and ingestion audit records
- SQLite: future app state such as watchlists and screener presets
- FastAPI: local backend API
- React/Vite: local frontend
- Kite Connect: first market-data provider
- Dhan and Upstox: future providers; preserve the shared provider interface so they can be added without rewriting ingestion, analytics, or storage
- Nifty 500: default active scan universe

Do not replace or delete the full Kite mapping when applying Nifty 500. Keep all provider mappings in `provider_instruments`; use `universe.is_active` to select the scan scope.

## Configuration and credentials

- Runtime settings: `config/settings.py`
- Public template: `.env.example`
- Local credentials: `.env` — gitignored; never print, commit, or copy its values into documentation.
- Provider selection: `DATA_PROVIDER=kite`
- Active universe: `UNIVERSE_SCOPE=nifty500`, `full_nse`, or `custom`

Kite authentication requires an `access_token`, not a `request_token`.

1. Kite login redirect returns a short-lived `request_token`.
2. Exchange it using the API key and API secret to receive an `access_token`.
3. Store the access token in `KITE__ACCESS_TOKEN`.
4. Kite access tokens normally expire at 6 AM the following day.

A future session may exchange a freshly pasted request token automatically, but must never display the request token, access token, API key, or API secret.

## Completed work

### Project scaffold

- Python package layout for ingestion, storage, analytics, scoring, backtesting, API, and scheduling
- React/Vite dashboard shell
- Docker Compose and Dockerfiles
- Scoring-weight configuration
- DuckDB and SQLite schemas

### Provider layer

- `DataProvider`, `Instrument`, and `DailyBar` live in `sepa_scanner/ingestion/providers/base.py`.
- `KiteDataProvider` lives in `sepa_scanner/ingestion/providers/kite.py`.
- `create_data_provider()` in `sepa_scanner/ingestion/providers/factory.py` selects the provider from settings.
- Kite calls are paced and retried.
- Historical date timestamps are normalized to calendar dates before DuckDB storage. Do not remove this conversion; it prevents one-day date shifts.

### Universe layer

- `sync_provider_instruments()` imports all Kite NSE cash-equity mappings.
- `apply_active_universe()` downloads and archives the official Nifty 500 constituent CSV, then marks matching symbols active.
- The Kite mapping contains 10,285 symbols.
- Current Nifty 500 snapshot requested 501 symbols; 499 currently match Kite mappings.
- `DUMMYHEG` is a constituent-file placeholder. `HFCL` currently has no matching Kite record. Keep these as reconciliation items rather than inventing a mapping.

### Ingestion and validation

- Daily OHLCV is upserted in bulk to DuckDB.
- Raw normalized response files are retained below `data/raw/<run-id>/`.
- Weekly bars are derived from stored daily bars.
- Ingestion records run metadata in `ingestion_log`.
- A five-symbol validation backfill from 2018 completed successfully for INFY, RELIANCE, TCS, HDFCBANK, and ICICIBANK.
- The validation re-run added zero rows, confirming idempotency.
- The initial row-by-row DuckDB approach was replaced with bulk upserts. Do not regress to row-by-row writes.
- Full Nifty 500 backfill completed in resumable batches. There are no pending active symbols.
- Current local coverage: 903,788 daily OHLCV rows and 190,471 derived weekly bars.

### Full-universe data quality validation

- Validation code: `sepa_scanner/ingestion/data_quality.py`.
- Latest report: `data/quality/quality-2026-09-26.json`.
- Passed: 0 invalid OHLC rows, 0 negative-price or negative-volume rows, and 0 daily-to-weekly reconciliation mismatches.
- Initial validation flagged 24 zero-volume bars, 1 stale close sequence (`TATAINVEST`, 11 sessions in April 2019), and 4,206 missing observed-market dates before listing-date metadata was populated.
- After listing-date-aware validation, the current report flags 486 missing dates. The earlier gaps for STARHEALTH, RAINBOW, MAZDOCK, LATENTVIEW, and HOMEFIRST were before their listing dates and are no longer flagged.
- Two failed `ingestion_log` runs are historical records from interrupted validation attempts. Corrected re-runs completed successfully.

## Current data-quality validation

- Missing-day validation starts at the later of a symbol's first stored bar and its known `universe.listing_date`, so dates before listing are not flagged.
- Per-symbol missing dates are persisted in `data/quality/quality-YYYY-MM-DD.json` under `missing_date_review_flags`.
- `sync_listing_dates()` in `sepa_scanner/ingestion/universe.py` refreshes listing dates from NSE's official equity master (`https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv`), matched by canonical symbol. Each downloaded source file is archived below `data/raw/universe/`.
- Current active symbols with daily data have listing dates populated. The validator reports listing-date coverage and uses each known listing date as the lower bound for expected bars.
- Symbols without listing metadata fall back to their first stored bar. Missing-day review flags are a data-quality signal, not proof of a provider failure; investigate them alongside listing and trading history.

## Phase 2 progress

- Daily and weekly Trend Template calculations are implemented in `sepa_scanner/analytics/trend_template.py`.
- Each of the eight criteria retains its measured values, threshold, and pass state. Insufficient history or an unavailable RS rating is represented as unevaluated rather than failed.
- Periods and thresholds are configurable in `config/trend_template.yaml`. Weekly windows are 10/30/40/52 bars, approximating the daily 50/150/200/252-session windows.
- Daily market-stage classification is implemented in `sepa_scanner/analytics/stage_classifier.py`. It stores the current active-universe stage with its moving-average measurements in DuckDB.
- Universe-wide RS ratings and equal-weight-relative RS-line history are implemented in `sepa_scanner/analytics/rs_rating.py`. The 3/6/9/12-month weighted returns and weights are configurable in `config/relative_strength.yaml`.
- The initial derived-data run stored 489 current stage classifications and 776,795 historical RS rows. `calculate_symbol_trend_template()` now reads the matching stored RS rating when one is available.
- Weekly-primary VCP detection with daily confirmation is implemented as a pure function in `sepa_scanner/analytics/vcp_detector.py` (`detect_vcp()` for a bars pair, `detect_symbol_vcp()` for one stored symbol), configurable via `config/vcp.yaml`. It computes swing highs/lows, contraction depth/volume decay, base length, pivot price, daily tight-close/breakout confirmation, and an A+/A/B/C quality grade.
- VCP detection is now persisted: `vcp_results_weekly` (in `storage/schema.sql`) stores one row per symbol per as-of date, including serialized `contractions_json`/`swings_json` for future chart overlays. `run_vcp_detection()` mirrors `run_market_stage_classification()` / `run_relative_strength_calculation()` — bulk-fetches adjusted-price weekly and daily bars for the active universe, evaluates each symbol, and upserts. `latest_vcp_results()` mirrors `latest_relative_strength_ratings()` for read access. Covered by `tests/test_vcp_persistence.py` (persistence, idempotent rerun, latest-row retrieval).
- Volume/supply-demand signals (design doc §6.5) are implemented in `sepa_scanner/analytics/volume_signals.py`: an up/down volume ratio, a Chaikin accumulation/distribution proxy with trend flag, and a volume-spike-near-pivot detector (the pivot is read from the latest persisted `vcp_results_weekly` row for that symbol). Configurable via `config/volume_signals.yaml`. Persisted per symbol/date in `volume_signals_daily` by `run_volume_signals()`; `latest_volume_signals()` mirrors the other `latest_*` read helpers. Covered by `tests/test_volume_signals.py` (pure-function unit tests with hand-verified small fixtures) and `tests/test_volume_signals_persistence.py` (persistence against the real default config).

## Phase 3 progress

- Config-driven gate and composite scoring is implemented in `sepa_scanner/scoring/scorer.py` (previously an empty stub). `calculate_composite_score()` is a pure function over already-computed analytics (trend template dicts, a `market_stages_daily` row, an `rs_ratings_daily` row, a `vcp_results_weekly` row, a `volume_signals_daily` row) — it does no data fetching or indicator math itself. `run_scoring()` bulk-fetches bars plus every other module's latest persisted rows for the active universe (mirroring `run_vcp_detection()` / `run_volume_signals()`) and upserts one row per symbol into the new `scores_daily` table (added to `storage/schema.sql`). `calculate_symbol_score()` is an on-demand single-symbol path for a future `/stock/{symbol}` endpoint. `latest_scores()` mirrors the other `latest_*` read helpers.
- The two hard gates from design doc §7.2 (Trend Template ≥ 6/8 fully-evaluated passes, RS ≥ 70) are config-driven from `scoring_weights.yaml`'s `gates` block and evaluated against the daily Trend Template only; the Trend Template *score* component averages daily + weekly pass rates per the design doc.
- The scorer loads `vcp.yaml` directly for the contraction-count bounds it needs (rather than duplicating them), per the existing comment in `scoring_weights.yaml`. New scorer-only config sections were added to `scoring_weights.yaml`: `stage_scores` (stage 1-4 → 0-100), `relative_strength.new_high_bonus`, `vcp_component_weights` (structure vs. daily-confirmation split), and `volume` (up/down-ratio normalization range + accumulation/spike bonuses).
- **Fundamentals scoring is deliberately excluded from the composite, not just unimplemented.** Decision (see chat history around 2026-09-27): fundamentals is a *qualifier/filter* on the technical setup (design doc §6.6: "more a qualifier than an equal-weight score input"), not a blended composite input — bad or missing fundamentals data should never be able to silently move a symbol's technical ranking, and blending both hypotheses (technical composite quality vs. fundamentals' incremental value) into one number makes them impossible to backtest independently. `scoring_weights.yaml`'s `weights.fundamentals` is explicitly set to `0.0` (not just left unevaluated) to enforce this at the config level regardless of whether a fundamentals engine ever runs. `tests/test_scorer.py::test_fundamentals_weight_is_zero_in_the_real_config` and `::test_fundamentals_weight_zero_means_an_evaluated_score_cannot_move_the_composite` lock this in as a regression check. There is still no Screener.in/NSE-XBRL/other fundamentals ingestion — `fundamentals_quarterly` stays empty and `_score_fundamentals()` always returns `(None, False)`.
- **When fundamentals scoring is eventually built** (per `SEPA_FUNDAMENTAL_QUALITY_ENGINE_SPEC.md`, uploaded 2026-09-27 — a more rigorous spec than what's summarized above, notably adding point-in-time correctness via `announcement_date` vs. `period_end`, piecewise/banded scoring instead of raw growth percentages, and EPS-acceleration-weighted sub-scoring matching Minervini's actual emphasis), it should: (1) resolve the data-source question first — Screener.in has **no official API** (confirmed via search 2026-09-27); NSE's own public-but-undocumented JSON API (the same access pattern `nsepython`/`jugaad-data` already use for years) plus a parser for NSE's 2024+ "Integrated Filing" XBRL format (an early-stage purpose-built library, `nse-xbrl`, exists for this) is the leading candidate over scraping a third party's proprietary content; (2) build the minimal v1 core (EPS growth + sales growth + ROE, banded, point-in-time-correct) before layering in acceleration/regime/consistency/deterioration-flags, matching this project's own "validate before adding sophistication" precedent for the VCP ML layer (design doc §9); (3) persist `fundamentals_score`/regime/flags for display and as a separate filter — it should surface in the UI and be usable to filter gate-passed technical setups, without ever entering the composite math unless a future backtest justifies raising `weights.fundamentals` above 0 as a conscious, documented change.
- **Data architecture is two-layer** (decided 2026-09-27): NSE/BSE exchange XBRL filings are the primary source of truth for `fundamentals_quarterly`; Screener.in is secondary and, by default, used only as a sampled cross-check (compares stored NSE/BSE values against Screener.in for a rotating sample of symbol-quarters, writes disagreements to a report, never writes to `fundamentals_quarterly`) — not a data source the scorer or ingestion depends on. `FundamentalsSettings` in `config/settings.py` (`fundamentals.fallback_source`, `fundamentals.cross_check_enabled`, `fundamentals.cross_check_sample_pct`; env vars `FUNDAMENTALS__FALLBACK_SOURCE`, `FUNDAMENTALS__CROSS_CHECK_ENABLED`, `FUNDAMENTALS__CROSS_CHECK_SAMPLE_PCT` in `.env.example`) is implemented and tested (`tests/test_settings.py`), ready for the ingestion module to read once built. A **Screener.in fallback exists as an explicit, off-by-default setting** (`fallback_source: "none" | "screener"`): when enabled, a symbol-quarter whose NSE/BSE XBRL filing fails to fetch or parse is filled from Screener.in for that one symbol-quarter only, and the resulting `fundamentals_quarterly` row **must** be tagged `source="screener_fallback"` (vs. `"nse_xbrl"`) so it never silently masquerades as verified exchange data downstream. This tagging requirement is a hard constraint on the eventual ingestion implementation, not optional polish — nothing has built it yet, but any implementation must honor it.
- Covered by `tests/test_scorer.py` (pure-function unit tests: gate pass/fail combinations, renormalization when components are missing, neutral-not-penalized handling of missing daily VCP confirmation and missing volume ratio, RS new-high-bonus capping, the fundamentals-weight-zero regression checks above), `tests/test_scorer_persistence.py` (`run_scoring()` persistence, idempotent rerun, `latest_scores()`, and `calculate_symbol_score()` against a real seeded uptrend symbol), and `tests/test_settings.py` (fundamentals fallback defaults to off, is overridable via env, cross-check defaults on and sampled). 40 tests across 9 files all pass as of the 2026-09-27 run.

- **NSE XBRL provider is implemented and verified against live data** (2026-09-27): `sepa_scanner/ingestion/providers/fundamentals_base.py` (the `FundamentalsProvider` protocol + `QuarterlyFiling` dataclass, mirroring `providers/base.py`'s `DataProvider`/`DailyBar` pattern) and `sepa_scanner/ingestion/providers/nse_xbrl.py` (`NSEXBRLProvider`, the concrete NSE adapter). This is the primary source of the two-layer fundamentals architecture.
  - **How it actually works, confirmed live, not just per documentation:** NSE has no official API, but `https://www.nseindia.com/api/corporates-financial-results?index=equities&period=Quarterly&symbol=X&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY` and the `xbrl` URLs it returns (hosted at `nsearchives.nseindia.com`) are reachable directly with a browser `User-Agent` and **no cookie/session warm-up** — simpler than `nsepython`/`jugaad-data`'s own approach for other NSE endpoints. The bare homepage (`https://www.nseindia.com/`) *is* blocked by Akamai bot mitigation (403), but that's irrelevant here since it's never fetched. **Always pass an explicit `from_date`/`to_date`** — an unbounded query for one symbol returned an incomplete, non-chronologically-sorted result set in testing; date-windowing is required for reliable results, not an optimization.
  - Parsing is done by the third-party `nse-xbrl` package (PyPI, `>=0.1.1`, added to `pyproject.toml`). Verified against real, live filings for RELIANCE, INFY, CDSL, and TATASTEEL: all four parse with plausible values matching known public financials (e.g. RELIANCE Q3 FY25 standalone: revenue ₹1,282,600,000,000, PAT ₹87,210,000,000, EPS ₹6.44, 13.53bn shares outstanding).
  - **Two real, confirmed data-quality findings that shape the eventual scoring engine, not hypothetical risks:** (1) **Banks parse without error but return `revenue`/`pat`/`basic_eps` as `None`** — tested live on HDFCBANK. Banks (and likely NBFCs) use a different XBRL taxonomy (interest income/NII, not "revenue from operations") that isn't mapped. Surfaced via `QuarterlyFiling.fields_missing=True`, never silently treated as zero. This confirms `SEPA_FUNDAMENTAL_QUALITY_ENGINE_SPEC.md`'s decision to defer sector-specific (bank/NBFC) handling was correct, with real evidence behind it now, not just caution. (2) **A live backlog filing was found**: a small-cap (`AHLWEST`) broadcast a quarterly result on 24-Aug-2026 for the **January-March 2021** period — announced more than five years after the period it reports on. This is concrete proof (not a theoretical edge case) that `announcement_date`-gated, point-in-time-correct scoring (already planned per the fundamentals spec) is essential: a system keyed on `period_end` would have silently treated five-year-old numbers as current.
  - A single filing that fails to parse (`FilingResult.from_xbrl()` raising) returns `None` from `_parse_filing()` rather than raising, so one bad filing can't crash a batch ingestion run over every other symbol — the eventual ingestion job is expected to log/flag `None` results, per this project's existing "flag, don't silently drop" rule.
  - Covered by `tests/test_nse_xbrl_provider.py`, which runs **entirely offline** against real filing XML saved to `tests/fixtures/nse_xbrl/` (fetched live 2026-09-27, so the suite never depends on NSE being reachable or unchanged): parsing RELIANCE/CDSL correctly, the HDFCBANK `fields_missing` flag, malformed-XBRL returning `None` not raising, the index-metadata date fallback, and the filing-index row-filtering/date-window-param logic (via a fake session, no real HTTP).
  - **Not yet built:** the actual ingestion module (`fundamentals_update.py`) that calls `NSEXBRLProvider`, computes YoY growth via self-join, and upserts into `fundamentals_quarterly`; the Screener.in cross-check/fallback provider; and `_score_fundamentals()` itself. `NSEXBRLProvider` is a verified, working building block, not a finished pipeline.

## Required next steps

1. Build fundamentals ingestion (`sepa_scanner/ingestion/fundamentals_update.py`) on top of the now-verified `NSEXBRLProvider`: upsert into `fundamentals_quarterly` (schema needs the `period_end`→`announcement_date` fields per the spec, plus a `source` column distinguishing `"nse_xbrl"` from `"screener_fallback"`), compute YoY growth at ingestion time via self-join, and handle the two confirmed edge cases (bank/NBFC `fields_missing` rows, backlog filings where `announcement_date` is far from `period_end`) explicitly rather than letting them pass through silently. Then build `_score_fundamentals()` per `SEPA_FUNDAMENTAL_QUALITY_ENGINE_SPEC.md`'s v1 core (EPS growth + sales growth + ROE, banded, point-in-time-correct) before layering in acceleration/regime/consistency/deterioration-flags. The two-layer architecture and its config (`FundamentalsSettings`, including the off-by-default Screener.in fallback) are already decided and implemented in `config/settings.py`. Remember: even once built, fundamentals stays out of the composite (`weights.fundamentals` stays `0.0`) unless a backtest justifies otherwise.
2. Add tests under `tests/` for RS rating and ingestion edge cases — these still do not have coverage.
3. Build API endpoints — `sepa_scanner/api/main.py` currently only exposes `/health` and `/settings/provider`, with `/universe` and `/scores/today` returning empty placeholders — and connect the frontend to real data. `latest_scores()` and `calculate_symbol_score()` in `sepa_scanner/scoring/scorer.py` are ready to back `/scores/today` and `/stock/{symbol}` respectively.
4. Wire `run_scoring()` into the daily pipeline/scheduler once one exists (Phase 7), after `run_market_stage_classification()`, `run_relative_strength_calculation()`, `run_vcp_detection()`, and `run_volume_signals()` — it depends on all four having already run for the same day.

## Implementation rules for future sessions

- Read `PROJECT-DESIGN.md` and this file first.
- Work phase by phase. Complete and validate the data layer before building analytics, scoring, or polished UI behavior.
- Keep calculations deterministic and explainable; store raw values behind each pass/fail or score.
- Keep scoring weights and pattern thresholds in YAML or settings, not hardcoded business logic.
- Weekly bars are primary for VCP structure. Daily bars refine entry confirmation.
- Preserve local-first operation. Avoid new infrastructure unless the design explicitly calls for it.
- Keep secrets solely in `.env`. Never show their contents in command output or chat.
- Use provider-neutral contracts. Adding Dhan or Upstox must mean adding an adapter and factory registration, not changing analytics or storage contracts.
- Do not silently drop questionable data. Flag or log it.
- Before a full external-data job, run a small controlled validation first.
- Before data writes that affect existing rows, explain the scope and preserve raw payloads where appropriate.
- Do not run a full backfill concurrently with another DuckDB writer.

## Helpful commands

```sh
# Activate local environment
source .venv/bin/activate

# Run the full-universe quality validation
.venv/bin/python -c "from sepa_scanner.ingestion.data_quality import run_data_quality_validation; print(run_data_quality_validation())"

# Check database coverage
.venv/bin/python -c "from sepa_scanner.storage.db import initialize_analytics; c=initialize_analytics(); print(c.execute('SELECT count(*) FROM ohlcv_daily').fetchone())"

# Run API locally
.venv/bin/uvicorn sepa_scanner.api.main:app --reload
```
