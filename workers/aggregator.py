import time
import logging
from datetime import datetime, timezone, timedelta
from db.writer import get_connection
from config import settings

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), "INFO"))
logger = logging.getLogger(__name__)

def run_aggregator():
    logger.info("Starting OHLCV 1s aggregator...")
    conn = get_connection()
    conn.autocommit = True
    
    # We will loop and aggregate the previous minute every few seconds
    # In a production environment with continuous aggregates in TimescaleDB,
    # this could be a materialized view. But as per spec, we do it in a worker.
    
    while True:
        try:
            # Query to aggregate trades into 1s buckets
            # We look back 10 seconds to catch delayed events
            now = datetime.now(timezone.utc)
            start_time = now - timedelta(seconds=10)
            
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ohlcv_1s (
                        bucket_start, exchange_code, symbol, native_symbol,
                        open, high, low, close, volume_base, volume_quote, trade_count, source, updated_at
                    )
                    SELECT
                        time_bucket('1 second', ts_event) AS bucket_start,
                        exchange_code,
                        symbol,
                        native_symbol,
                        (array_agg(price ORDER BY ts_event ASC))[1] AS open,
                        max(price) AS high,
                        min(price) AS low,
                        (array_agg(price ORDER BY ts_event DESC))[1] AS close,
                        sum(qty) AS volume_base,
                        sum(quote_qty) AS volume_quote,
                        count(*) AS trade_count,
                        'derived_trades' AS source,
                        now() AS updated_at
                    FROM trade_tick
                    WHERE ts_event >= %s AND ts_event < %s
                    GROUP BY bucket_start, exchange_code, symbol, native_symbol
                    ON CONFLICT (bucket_start, exchange_code, symbol)
                    DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume_base = EXCLUDED.volume_base,
                        volume_quote = EXCLUDED.volume_quote,
                        trade_count = EXCLUDED.trade_count,
                        updated_at = EXCLUDED.updated_at;
                    """,
                    (start_time, now)
                )
        except Exception as e:
            logger.error(f"Aggregator error: {e}")
        
        # Sleep for a short while before checking again
        time.sleep(2)

if __name__ == "__main__":
    try:
        run_aggregator()
    except KeyboardInterrupt:
        logger.info("Aggregator stopped.")
