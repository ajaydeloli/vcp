-- Analytical tables. Weekly bars are derived from daily bars during ingestion.
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    symbol VARCHAR NOT NULL,
    date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume BIGINT,
    delivery_pct DOUBLE,
    adj_close DOUBLE,
    source VARCHAR,
    ingested_at TIMESTAMP,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS ohlcv_weekly (
    symbol VARCHAR NOT NULL,
    week_end_date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume BIGINT,
    adj_close DOUBLE,
    PRIMARY KEY (symbol, week_end_date)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol VARCHAR NOT NULL,
    ex_date DATE NOT NULL,
    action_type VARCHAR NOT NULL,
    ratio VARCHAR
);

CREATE TABLE IF NOT EXISTS universe (
    symbol VARCHAR PRIMARY KEY,
    isin VARCHAR,
    sector VARCHAR,
    industry VARCHAR,
    listing_date DATE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

-- Point-in-time-correct quarterly fundamentals (PROJECT-CONTEXT.md "two-layer
-- fundamentals data architecture"). period_start/quarter_end describe the
-- reporting period; announcement_date is when the filing became public and
-- is what a future consumer must gate on, never quarter_end -- a confirmed
-- live case (AHLWEST) broadcast a quarter more than five years after its
-- period_end. announcement_lag_days / is_backlog_filing surface that gap
-- explicitly rather than silently treating an old filing as current.
-- eps_yoy_growth/sales_yoy_growth are computed at ingestion time (self-join
-- against the same symbol's row from ~4 quarters prior) so the scorer stays
-- a pure lookup like every other component; both are NULL, never 0, when
-- fields_missing is TRUE for either quarter (observed for banks/NBFCs,
-- whose XBRL taxonomy doesn't map to revenue/PAT/basic EPS).
-- roe is currently always NULL: QuarterlyFiling does not yet carry
-- shareholders' equity/net worth, so it cannot be computed from what the
-- NSE XBRL provider exposes today. Left as a column (per the design doc)
-- rather than dropped, so a future provider enhancement can fill it without
-- another schema migration.
CREATE TABLE IF NOT EXISTS fundamentals_quarterly (
    symbol VARCHAR NOT NULL,
    quarter_end DATE NOT NULL,
    period_start DATE,
    announcement_date DATE,
    eps DOUBLE,
    sales DOUBLE,
    roe DOUBLE,
    eps_yoy_growth DOUBLE,
    sales_yoy_growth DOUBLE,
    source VARCHAR,
    fields_missing BOOLEAN NOT NULL DEFAULT FALSE,
    announcement_lag_days INTEGER,
    is_backlog_filing BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at TIMESTAMP,
    -- True for rows sourced from a provider (Screener.in as of 2026-09-27)
    -- that doesn't expose a real regulatory broadcast date, so
    -- announcement_date is period_end + a configured typical lag rather
    -- than a verified date. is_backlog_filing can only be trusted as a
    -- genuine signal on a row where this is FALSE. See
    -- sepa_scanner/ingestion/providers/fundamentals_base.py.
    announcement_date_is_estimated BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (symbol, quarter_end)
);

-- Migration for a fundamentals_quarterly table created before the
-- announcement_date_is_estimated column existed (CREATE TABLE IF NOT
-- EXISTS above is a no-op against an already-created table, so a real
-- deployed database needs this explicit ADD COLUMN). Safe to run every
-- startup: IF NOT EXISTS makes it a no-op once the column is present.
-- DuckDB does not support a NOT NULL constraint on ALTER TABLE ADD COLUMN
-- ("Adding columns with constraints not yet supported"), so this column is
-- nullable on a migrated table even though it's NOT NULL DEFAULT FALSE on
-- a freshly created one; the DEFAULT FALSE still applies to existing rows
-- and every write path in fundamentals_update.py always supplies a value.
ALTER TABLE fundamentals_quarterly ADD COLUMN IF NOT EXISTS announcement_date_is_estimated BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS ingestion_log (
    run_id VARCHAR PRIMARY KEY,
    run_at TIMESTAMP NOT NULL,
    symbols_updated INTEGER,
    symbols_failed INTEGER,
    rows_added INTEGER,
    notes VARCHAR
);

-- Precomputed, universe-relative strength metrics. A row is retained for each
-- symbol/date so the UI and later scoring can show the RS line over time.
CREATE TABLE IF NOT EXISTS rs_ratings_daily (
  symbol VARCHAR NOT NULL,
  date DATE NOT NULL,
  composite_return DOUBLE NOT NULL,
  rating INTEGER NOT NULL,
  rs_line DOUBLE NOT NULL,
  rs_line_new_high BOOLEAN NOT NULL,
  universe_size INTEGER NOT NULL,
  PRIMARY KEY (symbol, date)
);

-- Stage classification is stored separately from raw price data and retains
-- the measured inputs used to produce each label.
CREATE TABLE IF NOT EXISTS market_stages_daily (
  symbol VARCHAR NOT NULL,
  date DATE NOT NULL,
  stage INTEGER NOT NULL,
  price DOUBLE NOT NULL,
  ma_short DOUBLE,
  ma_medium DOUBLE,
  ma_long DOUBLE,
  ma_long_slope DOUBLE,
  trend_template_passes INTEGER,
  PRIMARY KEY (symbol, date)
);

-- Source-specific mappings are separate from the canonical universe so providers can be swapped.
CREATE TABLE IF NOT EXISTS provider_instruments (
  provider VARCHAR NOT NULL,
  symbol VARCHAR NOT NULL,
  exchange VARCHAR NOT NULL,
  provider_instrument_id VARCHAR NOT NULL,
  segment VARCHAR NOT NULL,
  instrument_type VARCHAR NOT NULL,
  isin VARCHAR,
  name VARCHAR,
  refreshed_at TIMESTAMP NOT NULL,
  PRIMARY KEY (provider, symbol, exchange)
);

-- Weekly-primary VCP detection results, persisted per symbol/date so the UI can
-- render contraction/pivot chart overlays and the scorer can read the gate
-- outcomes without recomputing detect_symbol_vcp() on every request. One row
-- per symbol per as-of (latest weekly bar) date, refreshed by run_vcp_detection().
CREATE TABLE IF NOT EXISTS vcp_results_weekly (
  symbol VARCHAR NOT NULL,
  date DATE NOT NULL,
  is_vcp BOOLEAN NOT NULL,
  quality_grade VARCHAR NOT NULL,
  reason VARCHAR NOT NULL,
  pivot_price DOUBLE,
  pivot_date DATE,
  contraction_count INTEGER NOT NULL,
  base_length_weeks INTEGER,
  depth_decay_passed BOOLEAN NOT NULL,
  volume_decay_passed BOOLEAN NOT NULL,
  base_length_passed BOOLEAN NOT NULL,
  daily_confirmation_available BOOLEAN NOT NULL,
  near_pivot BOOLEAN,
  distance_to_pivot_fraction DOUBLE,
  tight_close_count INTEGER,
  tight_closes_confirmed BOOLEAN,
  breakout_confirmed BOOLEAN,
  -- Full swing/contraction point lists, serialized as JSON text, so a future
  -- Stock Detail chart can draw the exact overlay without recomputing it.
  contractions_json VARCHAR,
  swings_json VARCHAR,
  PRIMARY KEY (symbol, date)
);

-- Retains an auditable snapshot of each configured scan universe.
CREATE TABLE IF NOT EXISTS universe_memberships (
  universe_name VARCHAR NOT NULL,
  symbol VARCHAR NOT NULL,
  as_of_date DATE NOT NULL,
  source_url VARCHAR,
  retrieved_at TIMESTAMP NOT NULL,
  PRIMARY KEY (universe_name, symbol, as_of_date)
);

-- Volume and supply/demand signals (design doc §6.5): up/down volume ratio,
-- an accumulation/distribution proxy, and a volume-spike-near-pivot flag.
-- One row per symbol per as-of date, refreshed by run_volume_signals().
CREATE TABLE IF NOT EXISTS volume_signals_daily (
  symbol VARCHAR NOT NULL,
  date DATE NOT NULL,
  up_down_volume_ratio DOUBLE,
  up_volume DOUBLE,
  down_volume DOUBLE,
  accumulation_distribution_value DOUBLE,
  accumulation_distribution_rising BOOLEAN,
  pivot_price_used DOUBLE,
  volume_spike_near_pivot BOOLEAN,
  spike_date DATE,
  spike_volume DOUBLE,
  PRIMARY KEY (symbol, date)
);

-- Config-driven composite score (design doc §7), one row per symbol per as-of
-- date, refreshed by run_scoring(). Combines the previously-persisted trend
-- template, stage, RS, VCP, and volume component results into a single
-- 0-100 ranking number, with every component sub-score retained so the UI
-- can explain a score rather than show a single opaque number.
-- fundamentals_score / fundamentals_evaluated stay NULL/FALSE until
-- fundamentals ingestion exists (see PROJECT-CONTEXT.md); the composite is
-- renormalized across whichever components were actually evaluated.
CREATE TABLE IF NOT EXISTS scores_daily (
  symbol VARCHAR NOT NULL,
  date DATE NOT NULL,
  composite_score DOUBLE NOT NULL,
  gate_passed BOOLEAN NOT NULL,
  gate_trend_template_passed BOOLEAN,
  gate_relative_strength_passed BOOLEAN,
  trend_template_score DOUBLE,
  trend_template_evaluated BOOLEAN NOT NULL,
  stage_score DOUBLE,
  stage_evaluated BOOLEAN NOT NULL,
  relative_strength_score DOUBLE,
  relative_strength_evaluated BOOLEAN NOT NULL,
  vcp_score DOUBLE,
  vcp_evaluated BOOLEAN NOT NULL,
  volume_score DOUBLE,
  volume_evaluated BOOLEAN NOT NULL,
  fundamentals_score DOUBLE,
  fundamentals_evaluated BOOLEAN NOT NULL,
  quality_grade VARCHAR,
  stage INTEGER,
  relative_strength_rating INTEGER,
  PRIMARY KEY (symbol, date)
);
