-- PostgreSQL / TimescaleDB Initial Schema

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS exchange_ref (
    code            TEXT PRIMARY KEY,           -- 'binance', 'kraken', 'coinbase'
    name            TEXT NOT NULL,
    venue_type      TEXT NOT NULL DEFAULT 'cex',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market_ref (
    id              BIGSERIAL PRIMARY KEY,
    exchange_code   TEXT NOT NULL REFERENCES exchange_ref(code),
    symbol          TEXT NOT NULL,              -- canonique, ex: BTC/USDT
    native_symbol   TEXT NOT NULL,              -- ex: BTCUSDT / XBT/USD / BTC-USD
    base_asset      TEXT NOT NULL,
    quote_asset     TEXT NOT NULL,
    market_type     TEXT NOT NULL DEFAULT 'spot',
    status          TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    price_precision INTEGER,
    qty_precision   INTEGER,
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (exchange_code, native_symbol),
    UNIQUE (exchange_code, symbol)
);

CREATE TABLE IF NOT EXISTS trade_tick (
    ts_event        TIMESTAMPTZ NOT NULL,
    ts_ingested     TIMESTAMPTZ NOT NULL DEFAULT now(),
    exchange_code   TEXT NOT NULL REFERENCES exchange_ref(code),
    symbol          TEXT NOT NULL,
    native_symbol   TEXT NOT NULL,
    source_channel  TEXT NOT NULL,             -- aggTrade / trade / market_trades / ...
    event_uid       TEXT NOT NULL,             -- clé idempotente stable
    source_sequence BIGINT,
    trade_id        TEXT,
    side            TEXT NOT NULL DEFAULT 'unknown',
    price           NUMERIC(38, 18) NOT NULL,
    qty             NUMERIC(38, 18) NOT NULL,
    quote_qty       NUMERIC(38, 18),
    is_maker        BOOLEAN,
    payload         JSONB NOT NULL,
    PRIMARY KEY (ts_event, exchange_code, symbol, event_uid)
);

CREATE TABLE IF NOT EXISTS bbo_tick (
    ts_event        TIMESTAMPTZ NOT NULL,
    ts_ingested     TIMESTAMPTZ NOT NULL DEFAULT now(),
    exchange_code   TEXT NOT NULL REFERENCES exchange_ref(code),
    symbol          TEXT NOT NULL,
    native_symbol   TEXT NOT NULL,
    source_channel  TEXT NOT NULL,             -- bookTicker / ticker / level2_top / ...
    event_uid       TEXT NOT NULL,
    source_sequence BIGINT,
    bid_px          NUMERIC(38, 18) NOT NULL,
    bid_qty         NUMERIC(38, 18) NOT NULL,
    ask_px          NUMERIC(38, 18) NOT NULL,
    ask_qty         NUMERIC(38, 18) NOT NULL,
    payload         JSONB NOT NULL,
    PRIMARY KEY (ts_event, exchange_code, symbol, event_uid)
);

CREATE TABLE IF NOT EXISTS ohlcv_1s (
    bucket_start    TIMESTAMPTZ NOT NULL,
    exchange_code   TEXT NOT NULL REFERENCES exchange_ref(code),
    symbol          TEXT NOT NULL,
    native_symbol   TEXT NOT NULL,
    open            NUMERIC(38, 18) NOT NULL,
    high            NUMERIC(38, 18) NOT NULL,
    low             NUMERIC(38, 18) NOT NULL,
    close           NUMERIC(38, 18) NOT NULL,
    volume_base     NUMERIC(38, 18) NOT NULL DEFAULT 0,
    volume_quote    NUMERIC(38, 18) NOT NULL DEFAULT 0,
    trade_count     INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'derived_trades',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (bucket_start, exchange_code, symbol)
);

CREATE TABLE IF NOT EXISTS ingestion_checkpoint (
    collector_name      TEXT NOT NULL,         -- ex: binance-trades-shard-01
    shard_id            TEXT NOT NULL,
    cursor_text         TEXT,
    last_event_time     TIMESTAMPTZ,
    last_commit_time    TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collector_name, shard_id)
);

CREATE TABLE IF NOT EXISTS dead_letter_event (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    exchange_code       TEXT,
    symbol              TEXT,
    source_channel      TEXT,
    event_uid           TEXT,
    error_class         TEXT NOT NULL,
    error_message       TEXT NOT NULL,
    raw_payload         JSONB NOT NULL,
    resolved            BOOLEAN NOT NULL DEFAULT FALSE
);

SELECT create_hypertable('trade_tick', 'ts_event',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '1 day');

SELECT create_hypertable('bbo_tick', 'ts_event',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '1 day');

SELECT create_hypertable('ohlcv_1s', 'bucket_start',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '7 days');

CREATE INDEX IF NOT EXISTS idx_trade_tick_exchange_symbol_ts
    ON trade_tick (exchange_code, symbol, ts_event DESC);

CREATE INDEX IF NOT EXISTS idx_bbo_tick_exchange_symbol_ts
    ON bbo_tick (exchange_code, symbol, ts_event DESC);

CREATE INDEX IF NOT EXISTS idx_ohlcv_1s_exchange_symbol_ts
    ON ohlcv_1s (exchange_code, symbol, bucket_start DESC);

CREATE INDEX IF NOT EXISTS idx_trade_tick_payload_gin
    ON trade_tick USING GIN (payload jsonb_path_ops);

CREATE INDEX IF NOT EXISTS idx_dead_letter_created_at
    ON dead_letter_event (created_at DESC);

-- Rétention policies
SELECT add_retention_policy('trade_tick', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('bbo_tick', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('ohlcv_1s', INTERVAL '365 days', if_not_exists => TRUE);

-- Initial Exchanges
INSERT INTO exchange_ref (code, name, venue_type)
VALUES
  ('binance', 'Binance Spot', 'cex'),
  ('kraken', 'Kraken Spot', 'cex'),
  ('coinbase', 'Coinbase Advanced Trade', 'cex')
ON CONFLICT DO NOTHING;
