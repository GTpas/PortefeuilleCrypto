-- Continuous Aggregates for OHLCV 1-minute and 5-minute

-- OHLCV 1-minute aggregate
CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', bucket_start) AS bucket_1m,
    exchange_code,
    symbol,
    native_symbol,
    first(open, bucket_start) AS open,
    max(high) AS high,
    min(low) AS low,
    last(close, bucket_start) AS close,
    sum(volume_base) AS volume_base,
    sum(volume_quote) AS volume_quote,
    sum(trade_count) AS trade_count
FROM ohlcv_1s
GROUP BY bucket_1m, exchange_code, symbol, native_symbol
WITH NO DATA;

-- OHLCV 5-minute aggregate
CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_5m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', bucket_start) AS bucket_5m,
    exchange_code,
    symbol,
    native_symbol,
    first(open, bucket_start) AS open,
    max(high) AS high,
    min(low) AS low,
    last(close, bucket_start) AS close,
    sum(volume_base) AS volume_base,
    sum(volume_quote) AS volume_quote,
    sum(trade_count) AS trade_count
FROM ohlcv_1s
GROUP BY bucket_5m, exchange_code, symbol, native_symbol
WITH NO DATA;

-- Refresh policies: keep aggregates up to date automatically
SELECT add_continuous_aggregate_policy('ohlcv_1m',
    start_offset    => INTERVAL '10 minutes',
    end_offset      => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists   => TRUE);

SELECT add_continuous_aggregate_policy('ohlcv_5m',
    start_offset    => INTERVAL '30 minutes',
    end_offset      => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists   => TRUE);
