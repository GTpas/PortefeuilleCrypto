import json
import logging
import ccxt
from config import settings
from db.writer import get_connection
from psycopg2.extras import execute_values

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), "INFO"))
logger = logging.getLogger(__name__)

def precision_to_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None

def upsert_markets(conn, exchange_code: str, markets: dict):
    rows = []
    for _, m in markets.items():
        base = m.get("base")
        quote = m.get("quote")
        if not base or not quote:
            continue

        rows.append((
            exchange_code,
            m["symbol"],
            m.get("id") or m.get("symbol"),
            base,
            quote,
            m.get("type") or "spot",
            "ACTIVE" if m.get("active", True) else "INACTIVE",
            bool(m.get("active", True)),
            precision_to_int((m.get("precision") or {}).get("price")),
            precision_to_int((m.get("precision") or {}).get("amount")),
            json.dumps({
                "limits": m.get("limits"),
                "precision": m.get("precision"),
                "taker": m.get("taker"),
                "maker": m.get("maker")
            }),
        ))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO market_ref (
                exchange_code, symbol, native_symbol, base_asset, quote_asset,
                market_type, status, active, price_precision, qty_precision, meta
            ) VALUES %s
            ON CONFLICT (exchange_code, native_symbol)
            DO UPDATE SET
                symbol = EXCLUDED.symbol,
                base_asset = EXCLUDED.base_asset,
                quote_asset = EXCLUDED.quote_asset,
                market_type = EXCLUDED.market_type,
                status = EXCLUDED.status,
                active = EXCLUDED.active,
                price_precision = EXCLUDED.price_precision,
                qty_precision = EXCLUDED.qty_precision,
                meta = EXCLUDED.meta,
                updated_at = now();
            """,
            rows,
            page_size=500,
        )
    conn.commit()

def run_bootstrap():
    logger.info("Starting bootstrap worker...")
    conn = get_connection()
    try:
        for ex_id in settings.EXCHANGES:
            logger.info(f"Loading markets for {ex_id} via CCXT...")
            exchange_cls = getattr(ccxt, ex_id)
            exchange = exchange_cls({"enableRateLimit": True})
            markets = exchange.load_markets()
            upsert_markets(conn, ex_id, markets)
            logger.info(f"{ex_id}: {len(markets)} markets synchronized.")
    except Exception as e:
        logger.error(f"Bootstrap failed: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    run_bootstrap()
