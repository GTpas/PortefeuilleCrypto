-- Compression Policies for cold data

-- Enable compression on trade_tick (raw trades older than 7 days)
ALTER TABLE trade_tick SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange_code, symbol',
    timescaledb.compress_orderby = 'ts_event DESC'
);

SELECT add_compression_policy('trade_tick',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

-- Enable compression on bbo_tick (BBO older than 3 days)
ALTER TABLE bbo_tick SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange_code, symbol',
    timescaledb.compress_orderby = 'ts_event DESC'
);

SELECT add_compression_policy('bbo_tick',
    compress_after => INTERVAL '3 days',
    if_not_exists => TRUE);

-- Enable compression on ohlcv_1s (candles older than 30 days)
ALTER TABLE ohlcv_1s SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange_code, symbol',
    timescaledb.compress_orderby = 'bucket_start DESC'
);

SELECT add_compression_policy('ohlcv_1s',
    compress_after => INTERVAL '30 days',
    if_not_exists => TRUE);
