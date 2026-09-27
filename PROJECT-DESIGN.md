# PROJECT DESIGN — NSE VCP/SEPA Screener & Rating Engine

**Codename:** SEPA-Scanner
**Scope:** Personal-use research & screening tool (single user, no team, no SaaS)
**Version:** 2.0 (Merged Design)
**Last updated:** 25 Sep 2026

---

## 0. What Changed in This Version

This merges the original lean roadmap with the stronger SEPA/VCP methodology detail and
data-quality/calibration notes from a second draft, while deliberately **rejecting** the
enterprise-team architecture (DDD bounded contexts, microservices, CQRS, Redis Streams,
TimescaleDB, plugin systems) that draft proposed. You are one person building a personal tool —
the goal is to ship something correct and usable, not to defend an architecture in a design
review. Anything in that category is pushed to a clearly-marked **Deferred / Optional**
appendix (§14) so the ambition isn't lost, just not blocking v1.

---

## 1. Purpose & Scope

A personal, institution-grade research tool that:

1. Pulls and locally stores daily historical OHLCV data for all NSE-listed stocks via a market-data API.
2. Incrementally updates that local store every trading day (append-only, idempotent).
3. Runs Minervini's **Trend Template** (8-point filter), **market Stage classification**, and
   **VCP (Volatility Contraction Pattern)** detection — on **weekly charts as primary**, daily as
   the entry-timing confirmation layer.
4. Combines multiple weighted "passes" (trend quality, VCP quality, RS rating, fundamentals,
   volume/supply-demand) into a single **0–100 composite score** per stock.
5. Presents results in a modern local web UI: rankable/filterable watchlists, chart overlays
   showing detected contractions/pivot, and historical score tracking.

**Explicitly out of scope for v1:** trade execution/broker order placement, paper trading engine,
LLM chat assistant, multi-user support, plugin marketplace. These are real, useful ideas — they
live in §14 as a documented "later" track, not because they're bad, but because bundling them
into v1 is how solo projects stall at 30% done.

---

## 2. Guiding Design Principles

- **Data integrity first.** Every ingestion step is validated, deduplicated, and auditable.
- **Idempotent daily updates.** Re-running "today's update" twice must never corrupt data.
- **Deterministic, explainable scoring.** Rule-based only in v1 — no ML/black box. Every
  sub-score traceable to a specific number you can look up.
- **Rule-based VCP validated before any ML.** If you ever add ML-assisted pattern confidence,
  it only happens after the rule-based detector hits a solid recall rate against a labeled set
  (see §9). Don't build the fancy layer before the foundation works.
- **Local-first, minimal infrastructure.** One local database, no Redis/Postgres/message broker
  needed to run this on your own machine. Less to install, less to break, less to maintain alone.
- **Config-driven weights.** Scoring weights and thresholds live in a YAML file, not hardcoded.
- **Weekly-primary, daily-confirming.** Minervini reads VCP structure on weekly charts; daily is
  used for fine-tuning the actual entry near the pivot. The engine should support both timeframes
  from the start, not bolt weekly on later.

---

## 3. High-Level Architecture

```
┌──────────────┐   ┌──────────────┐   ┌───────────────────┐   ┌──────────────┐   ┌────────────┐
│ Data Source   │──▶│ Ingestion    │──▶│ Local Data Store   │──▶│ Analytics    │──▶│ Scoring     │
│ API (broker/  │   │ Service      │   │ (DuckDB + Parquet, │   │ Engine       │   │ Engine      │
│ Yahoo hybrid) │   │ (fetch+diff) │   │  SQLite for app    │   │ (TrendTmpl,  │   │ (0-100,     │
└──────────────┘   └──────────────┘   │  state/watchlists) │   │  Stage, VCP, │   │  weighted,  │
                                       └───────────────────┘   │  RS)         │   │  gated)     │
                                                                 └──────────────┘   └────┬───────┘
                                                                                          │
                                                                                   ┌──────▼───────┐
                                                                                   │ Backend API   │
                                                                                   │ (FastAPI)     │
                                                                                   └──────┬───────┘
                                                                                          │
                                                                                   ┌──────▼───────┐
                                                                                   │ Frontend UI   │
                                                                                   │ (React/Vite)  │
                                                                                   └──────────────┘
```

Runs entirely on your machine (or a small VPS later). A scheduler (APScheduler in-process, or OS
cron) triggers the daily pipeline after data-vendor settlement time post-market-close.

### 3.1 Module Layout (Modular, Not Micro-serviced)

A single Python codebase, cleanly separated by responsibility — enough structure to keep things
sane, not so much that every feature requires touching five layers:

```
sepa_scanner/
├── ingestion/        # fetch, diff, corporate actions, universe refresh
├── storage/           # DuckDB/SQLite schema + access helpers
├── analytics/         # trend template, stage classification, RS, VCP detector
├── scoring/            # weighted composite scorer, config-driven
├── backtest/           # calibration + forward-return validation (separate from live path)
├── api/                 # FastAPI app
├── frontend/            # React/Vite app
└── scheduler/            # daily pipeline runner
```

No bounded contexts, no DI container framework, no event bus. Python modules and plain function
calls/imports are sufficient at this scale — you can always extract a service boundary later if a
real need (not a hypothetical one) shows up.

---

## 4. Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Best ecosystem for quant/data (pandas, numpy, TA-Lib) |
| Historical/analytical storage | **DuckDB** + Parquet files on disk | Columnar, fast scans across thousands of tickers × years of daily/weekly bars, zero server, embeds like SQLite |
| App-state storage | **SQLite** | Watchlists, saved screener presets, user config, scan run metadata |
| Data fetch | Broker API (Kite Connect / Upstox / Dhan) as primary for clean, adjusted NSE OHLCV; Yahoo Finance as a free fallback/cross-check | Broker APIs give more reliable adjusted history than scraping; Yahoo is a reasonable free backup for sanity-checking |
| Fundamentals | Screener.in (primary), cross-checked against Moneycontrol where you want extra confidence | EPS/sales growth, ROE — used as a scoring component, not a hard gate by default |
| Universe / symbol master | **NSE official equity list**, cross-referenced against Zerodha's free daily instruments CSV | Yahoo/vendor symbol lists have real discrepancies; NSE's own list is canonical |
| Scheduling | APScheduler (in-process) or OS cron | No extra infra needed |
| Backend API | FastAPI | Async, typed, auto OpenAPI docs |
| Frontend | React + Vite + TypeScript, TailwindCSS, shadcn/ui, TradingView Lightweight Charts | Professional charting feel, modern component system |
| Technical indicators | pandas + TA-Lib / pandas_ta | Vectorized, no per-stock loops |
| Testing | pytest + hypothesis (pattern-detection edge cases) | — |
| Packaging | Poetry or uv | — |
| Containerization | Docker Compose (backend + frontend) | Easy local spin-up |

**Deliberately not used in v1:** PostgreSQL/TimescaleDB, Redis, Celery, a DI-container library,
message queues. None of these solve a problem you actually have yet at single-user, daily-batch
scale on thousands of tickers — DuckDB handles that comfortably on a laptop.

---

## 5. Data Layer

### 5.1 Data Source Strategy
- **Primary:** a broker API (Kite Connect, Upstox, or Dhan) for adjusted historical daily OHLCV —
  more reliable than scraping NSE directly, and you likely already need broker access anyway.
- **Secondary/cross-check:** Yahoo Finance, free and useful for spot-checking the primary feed.
- **Fundamentals:** Screener.in as primary, Moneycontrol as a cross-check when a number looks off.
- Keep the vendor behind a small `DataProvider` interface so swapping/adding a source later
  doesn't touch downstream analytics code.

### 5.2 Universe Definition
- Canonical symbol list = **NSE's official equity master list**, refreshed weekly.
- Cross-reference against **Zerodha's daily instruments CSV** (free) for clean, consistent
  symbol/ISIN mapping — this avoids the Yahoo-symbol-mismatch problem.
- Default universe: **Nifty 500**. Configurable to full NSE universe or a custom list.
- Maintain: symbol, ISIN, series (EQ only by default), sector/industry, listing date, active flag.
- Exclude illiquid/SME/ETF series unless explicitly included.

### 5.3 Storage Schema (DuckDB / Parquet)

```
ohlcv_daily (
  symbol TEXT, date DATE,
  open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,
  delivery_pct DOUBLE, adj_close DOUBLE,
  source TEXT, ingested_at TIMESTAMP,
  PRIMARY KEY (symbol, date)
)

ohlcv_weekly (            -- derived from daily via resampling, not separately fetched
  symbol TEXT, week_end_date DATE,
  open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,
  adj_close DOUBLE,
  PRIMARY KEY (symbol, week_end_date)
)

corporate_actions (
  symbol TEXT, ex_date DATE, action_type TEXT, ratio TEXT
)

universe (
  symbol TEXT PRIMARY KEY, isin TEXT, sector TEXT, industry TEXT,
  listing_date DATE, is_active BOOLEAN
)

fundamentals_quarterly (
  symbol TEXT, quarter_end DATE, eps DOUBLE, sales DOUBLE,
  eps_yoy_growth DOUBLE, sales_yoy_growth DOUBLE, roe DOUBLE
)

ingestion_log (
  run_id TEXT, run_at TIMESTAMP, symbols_updated INT,
  symbols_failed INT, rows_added INT, notes TEXT
)
```

Raw vendor payloads are kept in a `raw/` landing zone (as-received JSON/CSV) before transform, so
a parsing bug can be replayed without re-fetching from the vendor.

### 5.4 Ingestion & Incremental Update Logic
1. For every active symbol: find `MAX(date)` already stored, fetch only the delta range.
2. Validate: no negative prices, no zero volume on a trading day (flag, don't silently drop),
   OHLC consistency (`low <= open,close <= high`).
3. On a new corporate action, **retro-adjust the full history** for that symbol (`adj_close`) —
   this is a common silent-corruption source if skipped.
4. Upsert into DuckDB (`PRIMARY KEY (symbol, date)` prevents duplicate rows on re-run).
5. Resample daily → weekly bars (`ohlcv_weekly`) as part of the same run.
6. Log the run to `ingestion_log` for auditability.
7. **Idempotency is a hard requirement**: running the same day's update twice must be a no-op
   the second time.

### 5.5 Data Quality Checks (post-ingestion, pre-analytics)
- Missing-day detection vs the NSE trading calendar.
- Stale-price detection (price unchanged >10 sessions → possible feed issue vs genuine illiquidity).
- Large single-day % move without a matching corporate-action record → flag for review
  (likely an unadjusted split/bonus).

---

## 6. Analytics Engine

### 6.1 Trend Template (Minervini's 8 criteria — computed on both daily and weekly)
1. Price > 150-day MA and > 200-day MA
2. 150-day MA > 200-day MA
3. 200-day MA trending up for at least 1 month
4. 50-day MA > 150-day MA and > 200-day MA
5. Price > 50-day MA
6. Price at least 25–30% above 52-week low
7. Price within at least 25% of 52-week high (tightened to ~15% for the best setups)
8. RS Rating ≥ 70 (ideally 80–90+), computed vs the full configured universe

Each criterion stores both the boolean pass/fail and the underlying raw value, so the UI can
explain *why* a stock passed or failed — not just show a count.

### 6.2 Market Stage Classification
Classify each stock into Minervini's stage model, derived from the trend template outputs and MA
slope/positioning:
- **Stage 1** — Basing / accumulation (flat MAs, price chopping sideways)
- **Stage 2** — Advancing (the only stage you actually want to be buying in — trend template
  should be fully or mostly passing here)
- **Stage 3** — Topping / distribution (price extended, MAs flattening/rolling over)
- **Stage 4** — Declining

This gives a fast top-level filter before even looking at VCP structure: only Stage 2 candidates
are worth running the full pipeline on for entry purposes.

### 6.3 Relative Strength (RS) Rating
- Weighted blend of recent performance, more weight on recent quarter — Minervini/IBD-style:
  `(3-month return × 40%) + (6-month × 20%) + (9-month × 20%) + (12-month × 20%)`.
- Computed for every stock in the universe together, then **percentile-ranked 1–99** — RS is
  inherently relative, so it must be computed universe-wide each run, not per-symbol in isolation.
- Track the **RS line** itself (RS value over time) and flag when it makes a **new high before
  price does** — a genuine leading signal worth surfacing, not just the point-in-time percentile.

### 6.4 VCP (Volatility Contraction Pattern) Detection — Weekly Primary, Daily Confirming
This is the core algorithmic challenge, and the hardest to get exactly right. Approach:

1. **Primary detection on weekly bars.** Run swing high/low (zigzag/fractal) pivot detection over
   the weekly series across the lookback window (typically last 6 months to a bit over a year).
2. **Segment into contractions**: successive pullbacks where each pullback's depth is smaller than
   the previous (e.g. −25% → −15% → −8%).
3. **Contraction tolerance should not be strict.** Real-world VCPs rarely contract perfectly.
   Use a configurable tolerance — e.g. each contraction only needs to be roughly **≥80% as tight**
   as the previous one, not a strict 100% monotonic decrease. Make this a tunable parameter.
4. **Volume dry-up check**: average volume should decline through successive contractions. Use a
   **configurable range (40–60% of prior average)** rather than one hardcoded "50%" threshold —
   real data is noisy.
5. **Tightness near pivot**: narrowing daily/weekly ranges and contracting volume as price nears
   the pivot (prior resistance / high of the base).
6. **Daily confirmation layer**: once a weekly VCP structure is identified, switch to daily bars
   to time the actual entry — tight daily closes (within ~1–1.5%) near the pivot, and a
   volume-confirmed breakout above it.
7. **Pivot point**: the breakout trigger price — high of the most recent (tightest) contraction.
8. **Pattern sub-factors used in scoring**:
   - Number of contractions detected (2–4 ideal)
   - Depth-decay ratio between contractions
   - Volume-decay ratio between contractions
   - Base length in weeks (penalize <5wks or >65wks)
   - Distance from current price to pivot
   - Tight daily closes near pivot (count of closes within ~1–1.5% range)
9. **Setup quality grade**: roll the sub-factors into a simple A+/A/B/C label alongside the
   numeric score — useful as a quick visual filter in the UI on top of the raw number.

Expect several calibration passes here (see §9) — don't over-build before you have a labeled test
set to check against.

### 6.5 Supply/Demand & Volume Signals
- Up/down volume ratio over the last 50 sessions.
- Accumulation/distribution proxy.
- Volume spike on up-days near the pivot (institutional footprint signal).

### 6.6 Fundamentals Overlay
- EPS YoY growth, Sales YoY growth, margin trend, ROE — sourced quarterly from Screener.in.
- Used as a **scoring component by default**, with the option to configure it as a hard gate if
  you want a stricter full-SEPA filter (Minervini treats fundamentals more as a qualifier than an
  equal-weight score input — keep that as the default, but make it configurable).

---

## 7. Scoring Engine (0–100 Composite)

### 7.1 Structure
Weighted, config-driven (`config/scoring_weights.yaml`). Suggested defaults:

| Component | Weight | What it measures |
|---|---|---|
| Trend Template pass rate | 25% | Fraction of 8 criteria met, computed on weekly + daily |
| Stage classification | 10% | Bonus/penalty for being in Stage 2 vs other stages |
| RS Rating | 20% | Percentile rank vs universe, plus RS-line-new-high bonus |
| VCP Pattern Quality | 30% | Contraction count/decay, volume dry-up, base length, pivot proximity, tight-close count |
| Volume/Supply-Demand | 5% | Up/down volume ratio, accumulation signal |
| Fundamentals | 10% | EPS/Sales growth, ROE |

Each component normalized to 0–100 before weighting.

### 7.2 Hard Gates vs Soft Score
Two-stage filter, mirroring how Minervini actually uses the trend template:
1. **Gate**: must pass Trend Template ≥ 6/8 **and** RS ≥ 70 to be shown by default.
2. **Score**: among gated stocks, compute the weighted 0–100 composite for ranking.

This keeps a chart that merely *looks* VCP-shaped but fails the trend/RS qualifiers from ranking
highly by accident.

### 7.3 Score History
- Daily score snapshots per symbol (`scores_daily`), so the UI can show trend-of-score over time.
- Flag large score jumps (e.g. 55→85 overnight) as "new watchlist entrant" in the UI.

---

## 8. Backend API (FastAPI)

```
GET  /universe                          # tracked symbols + metadata
GET  /scores/today                      # ranked list, filterable (min score, sector, min RS...)
GET  /stock/{symbol}                    # OHLCV, trend template breakdown, stage, VCP contractions
                                         # (with coordinates for chart overlay), RS history, score history
GET  /stock/{symbol}/vcp-setups         # active + historical VCP setups for the symbol
POST /ingest/run                        # manually trigger a pipeline run
GET  /ingestion/status                  # last run time, errors, data freshness
GET  /watchlist        POST /watchlist  # user-curated lists
GET  /screener/presets POST /screener/presets  # saved filter presets
```

Auth: local API key or no-auth-on-localhost for v1; add real auth only if you expose this beyond
your own machine.

---

## 9. Backtesting & Calibration Framework

Since VCP detection is heuristic, you need a way to check it's actually working — kept as a
**separate module**, decoupled from the live scoring path so tuning experiments never touch
production data.

1. **Labeled dataset**: manually tag 50–100 known-good historical VCP setups (from Minervini's
   own books/case studies or your own chart review); confirm the detector flags them.
2. **Recall target before trusting the detector**: aim for the rule-based VCP detector to hit a
   solid recall rate (e.g. ≥80%) against the labeled set *before* considering any ML layer at all.
3. **Forward-return sanity check**: for historically ≥80-scored stocks, compare forward 1/3/6-month
   returns against the universe median — a calibration sanity check, not proof of alpha.
4. **Avoid lookahead bias religiously** — only use data available at the actual signal time; never
   use same-day closing price as the entry signal for that same day.
5. **Minimum 3-year backtest window** before trusting any parameter — a single year is misleading.
6. **Test across regimes**: include at least one sharp drawdown (e.g. 2020 COVID crash), one
   extended bear phase (e.g. 2022), and one recovery/bull phase (e.g. 2023) in the validation set.
7. **Parameter sweep**: grid-search contraction tolerance %, volume-decay range, and lookback
   window against the labeled set to lock in defaults.

---

## 10. Frontend UI

### 10.1 Screens
1. **Dashboard** — today's top-ranked stocks (0–100), sortable/filterable, sector heatmap.
2. **Stock Detail** — candlestick chart (weekly + daily toggle) with MA overlays (50/150/200),
   auto-annotated VCP contractions and pivot line, trend template checklist (pass/fail per
   criterion), stage badge, score breakdown (bar/radar), score history line.
3. **Screener** — build/save custom filters (RS ≥ X, min contractions, sector, stage, market cap).
4. **Watchlist** — curated list, score-trend sparklines, "new entrant" highlights.
5. **Data Health** — ingestion status, last update time, symbols with stale/missing data.

### 10.2 Style
- Dark-mode-first, data-dense but readable tables, card-based modern layout (shadcn/ui) rather
  than an old-fashioned dense grid.
- Score shown as a colored badge/gauge (red <50, yellow 50–75, green 75–100), setup quality shown
  as an A+/A/B/C tag next to the numeric VCP sub-score.

---

## 11. Performance Notes (Worth Building In From the Start)

- **Pre-compute indicators nightly** (MAs, ATR, RS, trend template, VCP detection) and store
  results — don't recompute per API request. For ~500–5,000 stocks × several indicators, this is
  a one-time nightly batch job, not something to run per page load.
- **Vectorize with pandas** across the whole universe at once — never loop symbol-by-symbol for
  MA/indicator calculations.
- **Cache screener results** in SQLite/memory, invalidated only when new data lands, not on every
  page view.

---

## 12. Project Roadmap

### Phase 0 — Setup (Week 1)
Repo scaffold, Poetry/uv env, Docker Compose skeleton, sign up for a broker API (Kite/Upstox/Dhan)
and confirm historical-data entitlements/rate limits. **This decision blocks everything else.**

### Phase 1 — Data Layer (Weeks 2–3)
`DataProvider` adapter, universe fetch/refresh (NSE list + Zerodha CSV cross-reference), bulk
historical backfill (5+ years), daily incremental update + idempotency tests, corporate-action
adjustment, weekly resampling, data quality checks, ingestion logging.
**Milestone:** daily update job runs twice with zero duplication, correct delta-only fetch.

### Phase 2 — Analytics Core (Weeks 4–6)
Trend Template (daily + weekly) with unit tests, Stage classification, RS Rating + RS line
tracking, pivot/swing detection, VCP contraction segmentation with configurable tolerances,
weekly-primary/daily-confirming logic.
**Milestone:** given a symbol, output structured JSON of contractions, trend template breakdown,
stage, RS rating.

### Phase 3 — Scoring Engine (Week 7)
Config-driven weights, gate+score pipeline, score history storage.
**Milestone:** daily run produces a ranked 0–100 list for the full universe.

### Phase 4 — Backtesting/Calibration (Weeks 8–9)
Labeled dataset, recall check against it, forward-return sanity checks, 3-year multi-regime test,
parameter tuning.
**Milestone:** documented calibration report; default thresholds locked for v1.

### Phase 5 — Backend API (Week 10)
Endpoints per §8, basic integration tests.

### Phase 6 — Frontend UI (Weeks 11–13)
Dashboard, Stock Detail (weekly/daily chart toggle + overlays), Screener, Watchlist, Data Health.

### Phase 7 — Scheduling & Ops (Week 14)
Automate daily pipeline post-close, alerting (email/Telegram) on ingestion failure or high-score
new entrants, backup strategy for the local DuckDB/Parquet store.

### Phase 8 — Hardening & Iteration (Ongoing)
Tighten fundamentals overlay, sector-relative RS, refine VCP tolerances based on live results.

---

## 13. Suggested Repo Structure

```
sepa-scanner/
├── ingestion/
│   ├── providers/            # broker + yahoo adapters
│   ├── universe.py
│   ├── daily_update.py
│   └── corporate_actions.py
├── storage/
│   ├── schema.sql
│   └── db.py
├── analytics/
│   ├── trend_template.py
│   ├── stage_classifier.py
│   ├── rs_rating.py
│   ├── vcp_detector.py
│   └── volume_signals.py
├── scoring/
│   ├── weights_config.yaml
│   └── scorer.py
├── backtest/
│   ├── labeled_examples/
│   └── evaluate.py
├── api/
│   └── main.py
├── frontend/
│   └── (React/Vite app)
├── scheduler/
│   └── run_daily_pipeline.py
├── tests/
├── docker-compose.yml
└── PROJECT-DESIGN.md
```

---

## 14. Deferred / Optional — Not Part of v1

Kept here deliberately so the ambition isn't lost, but none of this blocks or complicates the core
build above. Revisit only after Phases 0–8 are done and the scoring engine is trustworthy.

- **Paper trading engine** — simulate positions against live data once you trust the signals.
- **Broker execution integration** (Zerodha/AngelOne/Dhan order placement) — a much more
  carefully-scoped project on its own (risk controls, order management, failure handling); do not
  bolt this onto the screener casually.
- **LLM-powered setup explanations / chat assistant** — genuinely nice UX, but adds latency,
  cost, and a dependency surface that isn't needed to get a working screener.
- **ML-assisted VCP confidence scoring** — only after the rule-based detector's recall is
  validated (§9); treat as a research branch, not a core-path dependency.
- **Plugin/strategy-extension system** — solves a problem (multiple contributors, swappable
  strategies) you don't have as a solo user; a config file gets you tunability without the
  framework overhead.
- **Microservices split, message queue, Redis, TimescaleDB/PostgreSQL** — only worth it if this
  ever moves beyond single-user local/VPS use.
- **Multi-timeframe intraday screening, live tick data, WebSocket price feeds** — EOD-first is the
  right sequencing; intraday adds real complexity for a marginal gain until the EOD signal quality
  is proven.

---

## 15. Risks & Open Questions

1. **Broker API choice/cost** — resolve in Phase 0; blocks all ingestion work.
2. **VCP detection is inherently subjective** — budget for multiple calibration cycles against
   the labeled set; don't over-build before that set exists.
3. **Corporate action adjustment correctness** — bugs here silently corrupt trend/RS/VCP outputs;
   worth disproportionate test coverage.
4. **RS Rating universe choice** — Nifty 500 vs full NSE vs sector-relative changes rankings
   materially; pick one, document it, and don't compare scores across a universe change without
   re-running the whole history.
5. **This is a research/screening tool, not an execution system** — keep that boundary explicit
   even as §14 items get revisited later.

## 16. Next Steps

1. Pick and confirm the broker API for historical data (Phase 0).
2. Next artifact to build out in detail: either the DuckDB schema DDL + `DataProvider` interface,
   or the VCP detector pseudocode/spec at the algorithmic level — your call on which to start with.
