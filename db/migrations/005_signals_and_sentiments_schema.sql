-- Signals & Sentiments Traceability Schema

-- 1. Tracked sources & actors (Dimension tables)
CREATE TABLE IF NOT EXISTS tracked_source (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,       -- e.g., 'twitter', 'reddit', 'telegram', 'binance_announcements'
    type TEXT NOT NULL,              -- e.g., 'social_network', 'official_blog', 'news_aggregator'
    reliability_score NUMERIC(5,2) DEFAULT 0.5,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tracked_actor (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES tracked_source(id),
    handle TEXT NOT NULL,            -- e.g., '@elonmusk', 'r/cryptocurrency'
    actor_type TEXT NOT NULL,        -- e.g., 'founder', 'protocol_official', 'influencer', 'media'
    influence_score NUMERIC(5,2) DEFAULT 0.5,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(source_id, handle)
);

-- 2. Raw content ingestion
CREATE TABLE IF NOT EXISTS raw_content (
    id BIGSERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES tracked_source(id),
    actor_id INTEGER REFERENCES tracked_actor(id),
    source_url TEXT,                 -- Canonical URL
    content_hash TEXT NOT NULL UNIQUE, -- For deduplication
    raw_payload JSONB NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_raw_content_published_at ON raw_content (published_at DESC);

-- 3. Social Signal aggregates (Hypertable)
CREATE TABLE IF NOT EXISTS social_signal_1m (
    ts_bucket TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    s_social NUMERIC(10,4) NOT NULL,
    mention_velocity_z NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    sentiment_polarity NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    actor_influence_score NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    bot_risk_penalty NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    UNIQUE (ts_bucket, symbol)
);

SELECT create_hypertable('social_signal_1m', 'ts_bucket', if_not_exists => TRUE, chunk_time_interval => INTERVAL '7 days');
CREATE INDEX IF NOT EXISTS idx_social_signal_1m_symbol_ts ON social_signal_1m (symbol, ts_bucket DESC);

-- 4. Decision Tracking (Hypertables)
-- Replaces/upgrades the old signal_log
CREATE TABLE IF NOT EXISTS decision_snapshot (
    id BIGSERIAL,
    ts_eval TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol TEXT NOT NULL,
    exchange_code TEXT NOT NULL,
    s_social NUMERIC(10,4) NOT NULL,
    s_market NUMERIC(10,4) NOT NULL,
    s_risk NUMERIC(10,4) NOT NULL,
    s_total NUMERIC(10,4) NOT NULL,
    action_proposed TEXT NOT NULL, -- 'buy', 'sell', 'hold', 'reduce'
    confidence_score NUMERIC(5,2) DEFAULT 1.0,
    portfolio_id INTEGER, -- Optional link to which portfolio this decision was evaluated for
    PRIMARY KEY (id, ts_eval)
);

SELECT create_hypertable('decision_snapshot', 'ts_eval', if_not_exists => TRUE, chunk_time_interval => INTERVAL '30 days');
CREATE INDEX IF NOT EXISTS idx_decision_snapshot_symbol_ts ON decision_snapshot (symbol, ts_eval DESC);

-- Factor breakdown for explainability
CREATE TABLE IF NOT EXISTS decision_factor (
    id BIGSERIAL,
    ts_eval TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision_snapshot_id BIGINT NOT NULL,
    factor_category TEXT NOT NULL, -- 'social', 'market', 'risk'
    factor_name TEXT NOT NULL,     -- e.g., 'spread_bps', 'mention_velocity_z'
    factor_value NUMERIC(10,4) NOT NULL, -- Raw value of the metric
    score_contribution NUMERIC(10,4) NOT NULL, -- How much it added/subtracted to S_total
    explanation TEXT,              -- Human readable explanation, e.g., "Spread is tight (2bps)"
    PRIMARY KEY (id, ts_eval)
);

SELECT create_hypertable('decision_factor', 'ts_eval', if_not_exists => TRUE, chunk_time_interval => INTERVAL '30 days');
CREATE INDEX IF NOT EXISTS idx_decision_factor_snapshot_id ON decision_factor (decision_snapshot_id, ts_eval DESC);

-- Link evidence (e.g., social posts) to decisions
CREATE TABLE IF NOT EXISTS decision_evidence_link (
    id BIGSERIAL,
    ts_eval TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision_snapshot_id BIGINT NOT NULL,
    raw_content_id BIGINT NOT NULL,
    relevance_score NUMERIC(5,2) DEFAULT 1.0,
    PRIMARY KEY (id, ts_eval)
);

SELECT create_hypertable('decision_evidence_link', 'ts_eval', if_not_exists => TRUE, chunk_time_interval => INTERVAL '30 days');
CREATE INDEX IF NOT EXISTS idx_decision_evidence_snapshot ON decision_evidence_link (decision_snapshot_id, ts_eval DESC);

-- Initialize base sources
INSERT INTO tracked_source (name, type, reliability_score) VALUES 
('twitter', 'social_network', 0.6),
('reddit', 'social_network', 0.7),
('telegram', 'social_network', 0.5)
ON CONFLICT (name) DO NOTHING;
