-- ============================================================
-- 007 — Signals & Sentiments v2 Schema Extension
-- Idempotent: all CREATE IF NOT EXISTS / DO $$ guards
-- ============================================================

-- ────────────────────────────────────────────
-- 1. Tracked Sites (official blogs, status pages, governance forums)
-- ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tracked_site (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,           -- e.g. 'binance_blog', 'ethereum_governance'
    url             TEXT NOT NULL,                   -- canonical URL
    site_type       TEXT NOT NULL,                   -- 'blog', 'status_page', 'governance', 'listing', 'security', 'regulatory'
    related_assets  TEXT[] DEFAULT '{}',             -- e.g. {'ETH','BTC'}
    check_interval  INTERVAL DEFAULT '5 minutes',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    reliability_score NUMERIC(5,2) DEFAULT 0.7,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ────────────────────────────────────────────
-- 2. Asset ↔ Source mapping
-- ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tracked_asset_source_map (
    id              SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,                   -- canonical symbol e.g. 'BTC/USDT'
    base_asset      TEXT NOT NULL,                   -- e.g. 'BTC'
    source_id       INTEGER REFERENCES tracked_source(id),
    actor_id        INTEGER REFERENCES tracked_actor(id),
    site_id         INTEGER REFERENCES tracked_site(id),
    relevance       NUMERIC(5,2) DEFAULT 1.0,       -- how relevant this source is for this asset
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(symbol, source_id, actor_id, site_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_source_map_symbol
    ON tracked_asset_source_map (symbol);

-- ────────────────────────────────────────────
-- 3. Content Entity extraction (assets/tickers detected in raw_content)
-- ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS content_entity (
    id              BIGSERIAL PRIMARY KEY,
    raw_content_id  BIGINT NOT NULL REFERENCES raw_content(id) ON DELETE CASCADE,
    entity_type     TEXT NOT NULL,                   -- 'asset', 'actor', 'narrative', 'event'
    entity_value    TEXT NOT NULL,                   -- e.g. 'BTC', '@vitalik', 'ETF_approval'
    entity_confidence NUMERIC(5,2) NOT NULL DEFAULT 0.5,
    content_type    TEXT,                            -- 'announcement', 'rumor', 'listing', 'security_incident', 'governance', 'regulation', 'hype', 'market_commentary'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_content_entity_raw_content
    ON content_entity (raw_content_id);
CREATE INDEX IF NOT EXISTS idx_content_entity_value
    ON content_entity (entity_value, entity_type);

-- ────────────────────────────────────────────
-- 4. Market Feature 1s (hypertable)
-- ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_feature_1s (
    ts              TIMESTAMPTZ NOT NULL,
    symbol          TEXT NOT NULL,
    exchange_code   TEXT NOT NULL,
    spread_bps      NUMERIC(10,4) DEFAULT 0,
    depth_usd_10bps NUMERIC(18,2) DEFAULT 0,
    book_imbalance  NUMERIC(10,6) DEFAULT 0,        -- [-1, +1]
    trade_pressure  NUMERIC(10,6) DEFAULT 0,        -- [-1, +1] buy vs sell pressure
    relative_volume NUMERIC(10,4) DEFAULT 1.0,
    slippage_bps_est NUMERIC(10,4) DEFAULT 0,
    bid_px          NUMERIC(38,18),
    ask_px          NUMERIC(38,18),
    mid_px          NUMERIC(38,18),
    PRIMARY KEY (ts, symbol, exchange_code)
);

SELECT create_hypertable('market_feature_1s', 'ts',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '1 day');

CREATE INDEX IF NOT EXISTS idx_market_feature_1s_symbol_ts
    ON market_feature_1s (symbol, ts DESC);

-- Compression
ALTER TABLE market_feature_1s SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange_code, symbol',
    timescaledb.compress_orderby = 'ts DESC'
);

SELECT add_compression_policy('market_feature_1s',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('market_feature_1s',
    INTERVAL '90 days',
    if_not_exists => TRUE);

-- ────────────────────────────────────────────
-- 5. Market Feature 1m (continuous aggregate)
-- ────────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS market_feature_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', ts) AS bucket_1m,
    symbol,
    exchange_code,
    avg(spread_bps)         AS avg_spread_bps,
    avg(depth_usd_10bps)    AS avg_depth_usd_10bps,
    avg(book_imbalance)     AS avg_book_imbalance,
    avg(trade_pressure)     AS avg_trade_pressure,
    avg(relative_volume)    AS avg_relative_volume,
    avg(slippage_bps_est)   AS avg_slippage_bps_est,
    last(mid_px, ts)        AS last_mid_px,
    first(mid_px, ts)       AS first_mid_px,
    max(mid_px)             AS high_mid_px,
    min(mid_px)             AS low_mid_px
FROM market_feature_1s
GROUP BY bucket_1m, symbol, exchange_code
WITH NO DATA;

SELECT add_continuous_aggregate_policy('market_feature_1m',
    start_offset    => INTERVAL '10 minutes',
    end_offset      => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists   => TRUE);

-- ────────────────────────────────────────────
-- 6. Social Signal 5m (hypertable)
-- ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS social_signal_5m (
    ts_bucket           TIMESTAMPTZ NOT NULL,
    symbol              TEXT NOT NULL,
    s_social            NUMERIC(10,4) NOT NULL,
    mention_velocity_z  NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    sentiment_polarity  NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    unique_authors      INTEGER NOT NULL DEFAULT 0,
    engagement_velocity NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    cross_source_confirm NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    novelty_score       NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    actor_influence_score NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    bot_risk_penalty    NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    source_breakdown    JSONB DEFAULT '{}'::jsonb,   -- {"twitter": 0.3, "reddit": 0.5, ...}
    UNIQUE (ts_bucket, symbol)
);

SELECT create_hypertable('social_signal_5m', 'ts_bucket',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '7 days');

CREATE INDEX IF NOT EXISTS idx_social_signal_5m_symbol_ts
    ON social_signal_5m (symbol, ts_bucket DESC);

SELECT add_retention_policy('social_signal_5m',
    INTERVAL '180 days',
    if_not_exists => TRUE);

-- ────────────────────────────────────────────
-- 7. Enrich social_signal_1m with missing columns
-- ────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='social_signal_1m' AND column_name='unique_authors') THEN
        ALTER TABLE social_signal_1m ADD COLUMN unique_authors INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='social_signal_1m' AND column_name='engagement_velocity') THEN
        ALTER TABLE social_signal_1m ADD COLUMN engagement_velocity NUMERIC(10,4) DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='social_signal_1m' AND column_name='cross_source_confirm') THEN
        ALTER TABLE social_signal_1m ADD COLUMN cross_source_confirm NUMERIC(10,4) DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='social_signal_1m' AND column_name='novelty_score') THEN
        ALTER TABLE social_signal_1m ADD COLUMN novelty_score NUMERIC(10,4) DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='social_signal_1m' AND column_name='source_breakdown') THEN
        ALTER TABLE social_signal_1m ADD COLUMN source_breakdown JSONB DEFAULT '{}'::jsonb;
    END IF;
END $$;

-- ────────────────────────────────────────────
-- 8. Portfolio State history (hypertable)
-- ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS portfolio_state (
    ts              TIMESTAMPTZ NOT NULL,
    portfolio_id    INTEGER NOT NULL,
    total_value     NUMERIC(38,18) NOT NULL,
    current_cash    NUMERIC(38,18) NOT NULL,
    invested_value  NUMERIC(38,18) NOT NULL DEFAULT 0,
    num_positions   INTEGER NOT NULL DEFAULT 0,
    max_position_weight NUMERIC(10,4) DEFAULT 0,
    portfolio_vol   NUMERIC(10,6) DEFAULT 0,
    btc_corr        NUMERIC(10,6) DEFAULT 0,
    drawdown_pct    NUMERIC(10,4) DEFAULT 0,
    exposure_pct    NUMERIC(10,4) DEFAULT 0,
    positions_snapshot JSONB DEFAULT '[]'::jsonb,
    PRIMARY KEY (ts, portfolio_id)
);

SELECT create_hypertable('portfolio_state', 'ts',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '30 days');

CREATE INDEX IF NOT EXISTS idx_portfolio_state_portfolio_ts
    ON portfolio_state (portfolio_id, ts DESC);

ALTER TABLE portfolio_state SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'portfolio_id',
    timescaledb.compress_orderby = 'ts DESC'
);

SELECT add_compression_policy('portfolio_state',
    compress_after => INTERVAL '30 days',
    if_not_exists => TRUE);

-- ────────────────────────────────────────────
-- 9. Signal Quality Audit
-- ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signal_quality_audit (
    id              BIGSERIAL,
    ts_eval         TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision_snapshot_id BIGINT NOT NULL,
    symbol          TEXT NOT NULL,
    social_sources_count INTEGER NOT NULL DEFAULT 0,
    market_data_age_ms   INTEGER NOT NULL DEFAULT 0,     -- how stale is the latest market data
    social_data_age_ms   INTEGER NOT NULL DEFAULT 0,
    has_sufficient_social BOOLEAN NOT NULL DEFAULT FALSE,
    has_sufficient_market BOOLEAN NOT NULL DEFAULT FALSE,
    quality_grade    TEXT NOT NULL DEFAULT 'degraded',    -- 'full', 'partial', 'degraded', 'mock'
    degradation_reasons TEXT[] DEFAULT '{}',
    PRIMARY KEY (id, ts_eval)
);

SELECT create_hypertable('signal_quality_audit', 'ts_eval',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '30 days');

CREATE INDEX IF NOT EXISTS idx_signal_quality_audit_snapshot
    ON signal_quality_audit (decision_snapshot_id, ts_eval DESC);

-- ────────────────────────────────────────────
-- 10. Outcome Evaluation (ex-post decision quality)
-- ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS outcome_eval (
    id              BIGSERIAL,
    ts_eval         TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision_snapshot_id BIGINT NOT NULL,
    symbol          TEXT NOT NULL,
    horizon         TEXT NOT NULL,                       -- '1h', '4h', '24h', '3d'
    price_at_decision NUMERIC(38,18),
    price_at_horizon  NUMERIC(38,18),
    return_pct      NUMERIC(10,4),
    was_correct     BOOLEAN,                             -- did the action align with outcome?
    PRIMARY KEY (id, ts_eval)
);

SELECT create_hypertable('outcome_eval', 'ts_eval',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '30 days');

CREATE INDEX IF NOT EXISTS idx_outcome_eval_snapshot
    ON outcome_eval (decision_snapshot_id, ts_eval DESC);

-- ────────────────────────────────────────────
-- 11. Source Influence Snapshot (actor credibility over time)
-- ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS source_influence_snapshot (
    id              BIGSERIAL,
    ts_eval         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id        INTEGER NOT NULL REFERENCES tracked_actor(id),
    influence_score NUMERIC(5,2) NOT NULL,
    historical_lift NUMERIC(10,4) DEFAULT 0,             -- avg price lift after actor's posts
    accuracy_rate   NUMERIC(5,2) DEFAULT 0,              -- % of correct calls
    total_mentions  INTEGER DEFAULT 0,
    PRIMARY KEY (id, ts_eval)
);

SELECT create_hypertable('source_influence_snapshot', 'ts_eval',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '90 days');

-- ────────────────────────────────────────────
-- 12. Add decision_snapshot_id to paper_trade if missing
-- ────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='paper_trade' AND column_name='decision_snapshot_id') THEN
        ALTER TABLE paper_trade ADD COLUMN decision_snapshot_id BIGINT;
    END IF;
END $$;

-- ────────────────────────────────────────────
-- 13. Enrich decision_snapshot with reason_code and decision_confidence
-- ────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='decision_snapshot' AND column_name='reason_code') THEN
        ALTER TABLE decision_snapshot ADD COLUMN reason_code TEXT DEFAULT 'hold';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='decision_snapshot' AND column_name='quality_grade') THEN
        ALTER TABLE decision_snapshot ADD COLUMN quality_grade TEXT DEFAULT 'mock';
    END IF;
END $$;

-- ────────────────────────────────────────────
-- 14. Add more initial tracked sources
-- ────────────────────────────────────────────
INSERT INTO tracked_source (name, type, reliability_score) VALUES
('truth_social', 'social_network', 0.3),
('official_blog', 'official_site', 0.9),
('governance_forum', 'governance', 0.8),
('mock_social', 'mock', 0.5)
ON CONFLICT (name) DO NOTHING;
