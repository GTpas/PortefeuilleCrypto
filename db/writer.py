import json
import logging
from typing import List, Union
import psycopg2
from psycopg2.extras import execute_values
from config import settings
from models.canonical import TradeTick, BBOTick

logger = logging.getLogger(__name__)

def get_connection():
    return psycopg2.connect(settings.DATABASE_URL)

def flush_trades(conn, trades: List[TradeTick]):
    if not trades:
        return
    rows = []
    for t in trades:
        rows.append((
            t.ts_event, t.ts_ingested, t.exchange_code, t.symbol, t.native_symbol,
            t.source_channel, t.event_uid, t.source_sequence, t.trade_id, t.side,
            t.price, t.qty, t.quote_qty, t.is_maker, json.dumps(t.payload)
        ))
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO trade_tick (
                ts_event, ts_ingested, exchange_code, symbol, native_symbol,
                source_channel, event_uid, source_sequence, trade_id, side,
                price, qty, quote_qty, is_maker, payload
            ) VALUES %s
            ON CONFLICT (ts_event, exchange_code, symbol, event_uid) DO NOTHING;
            """,
            rows,
            page_size=1000
        )

def flush_bbos(conn, bbos: List[BBOTick]):
    if not bbos:
        return
    rows = []
    for b in bbos:
        rows.append((
            b.ts_event, b.ts_ingested, b.exchange_code, b.symbol, b.native_symbol,
            b.source_channel, b.event_uid, b.source_sequence,
            b.bid_px, b.bid_qty, b.ask_px, b.ask_qty, json.dumps(b.payload)
        ))
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO bbo_tick (
                ts_event, ts_ingested, exchange_code, symbol, native_symbol,
                source_channel, event_uid, source_sequence,
                bid_px, bid_qty, ask_px, ask_qty, payload
            ) VALUES %s
            ON CONFLICT (ts_event, exchange_code, symbol, event_uid) DO NOTHING;
            """,
            rows,
            page_size=1000
        )

def write_to_dlq(conn, error_class: str, error_message: str, payload: dict, exchange_code: str = None, symbol: str = None, event_uid: str = None, source_channel: str = None):
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dead_letter_event (
                    exchange_code, symbol, source_channel, event_uid,
                    error_class, error_message, raw_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (exchange_code, symbol, source_channel, event_uid, error_class, error_message, json.dumps(payload))
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to write to DLQ: {e}")

class DatabaseWriter:
    def __init__(self):
        self.conn = get_connection()
        self.conn.autocommit = False

    def write_batch(self, trades: List[TradeTick], bbos: List[BBOTick]):
        try:
            flush_trades(self.conn, trades)
            flush_bbos(self.conn, bbos)
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Database write failed: {e}. Writing to DLQ...")
            # If batch fails, write events to DLQ (simplified: just taking repr of batch for error)
            # In a real prod environment, we might retry individually or binary search the failing row.
            for t in trades:
                write_to_dlq(self.conn, e.__class__.__name__, str(e), t.payload, t.exchange_code, t.symbol, t.event_uid, t.source_channel)
            for b in bbos:
                write_to_dlq(self.conn, e.__class__.__name__, str(e), b.payload, b.exchange_code, b.symbol, b.event_uid, b.source_channel)

    def close(self):
        self.conn.close()
