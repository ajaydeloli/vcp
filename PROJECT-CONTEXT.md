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
- **Fundamentals scoring is not implemented** — there is still no Screener.in (or other) fundamentals ingestion, so `fundamentals_quarterly` stays empty. `_score_fundamentals()` always returns `(None, False)`; the composite renormalizes across whichever components were actually evaluated for a symbol rather than scoring the missing 10% weight as zero. This is a known gap, not an oversight — a future session should either build minimal fundamentals ingestion or explicitly decide to ship v1 without it.
- Covered by `tests/test_scorer.py` (pure-function unit tests: gate pass/fail combinations, renormalization when components are missing, neutral-not-penalized handling of missing daily VCP confirmation and missing volume ratio, RS new-high-bonus capping) and `tests/test_scorer_persistence.py` (`run_scoring()` persistence, idempotent rerun, `latest_scores()`, and `calculate_symbol_score()` against a real seeded uptrend symbol). 35 tests across 8 files all pass as of the 2026-09-27 run.

## Required next steps

1. Decide and implement fundamentals ingestion (Screener.in per the design doc, §6.6) — or explicitly document that v1 ships with fundamentals unevaluated. Either way, update this file once decided.
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
