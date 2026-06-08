import asyncio
import logging
import time
from typing import List

from config import settings
from db.writer import DatabaseWriter
from collectors.binance import BinanceCollector
from collectors.kraken import KrakenCollector
from collectors.coinbase import CoinbaseCollector
from models.canonical import TradeTick, BBOTick
from metrics import (
    start_metrics_server, market_events_total, queue_depth, db_write_latency_ms,
)

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), "INFO"))
logger = logging.getLogger(__name__)

# Bounded in-memory queues (back-pressure). Size is the configured MAX_MARKET_EVENTS
# so the bound is real and tunable, not a hardcoded literal.
trade_queue: asyncio.Queue = asyncio.Queue(maxsize=settings.MAX_MARKET_EVENTS)
bbo_queue: asyncio.Queue = asyncio.Queue(maxsize=settings.MAX_MARKET_EVENTS)

def handle_trade(trade: TradeTick):
    try:
        trade_queue.put_nowait(trade)
        market_events_total.labels(exchange=trade.exchange_code, kind="trade").inc()
    except asyncio.QueueFull:
        logger.warning("Trade queue full, dropping event")

def handle_bbo(bbo: BBOTick):
    try:
        bbo_queue.put_nowait(bbo)
        market_events_total.labels(exchange=bbo.exchange_code, kind="bbo").inc()
    except asyncio.QueueFull:
        logger.warning("BBO queue full, dropping event")

def sync_db_write(trades: List[TradeTick], bbos: List[BBOTick]):
    # Note: creates a new connection per batch or reuses.
    # It's better to instantiate it in the thread or keep it around.
    # We will instantiate writer per thread execution for safety, or keep a thread-local one.
    writer = DatabaseWriter()
    try:
        t0 = time.time()
        writer.write_batch(trades, bbos)
        db_write_latency_ms.observe((time.time() - t0) * 1000.0)
    finally:
        writer.close()

async def db_writer_loop():
    logger.info("Starting DB writer loop...")
    last_flush = time.time()
    trades_buffer = []
    bbos_buffer = []

    while True:
        now = time.time()

        queue_depth.labels(queue="trade").set(trade_queue.qsize())
        queue_depth.labels(queue="bbo").set(bbo_queue.qsize())

        # Drain queues opportunistically
        while len(trades_buffer) < settings.BATCH_MAX_ROWS:
            try:
                trades_buffer.append(trade_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        while len(bbos_buffer) < settings.BATCH_MAX_ROWS:
            try:
                bbos_buffer.append(bbo_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        should_flush = (
            len(trades_buffer) >= settings.BATCH_MAX_ROWS
            or len(bbos_buffer) >= settings.BATCH_MAX_ROWS
            or (now - last_flush) >= settings.FLUSH_EVERY_SECONDS
        )

        if should_flush and (trades_buffer or bbos_buffer):
            trades_to_write = list(trades_buffer)
            bbos_to_write = list(bbos_buffer)
            trades_buffer.clear()
            bbos_buffer.clear()
            last_flush = time.time()

            # Execute blocking write in a separate thread
            await asyncio.to_thread(sync_db_write, trades_to_write, bbos_to_write)

        await asyncio.sleep(0.01)

async def main():
    logger.info("Initializing Live Ingestor...")

    start_metrics_server(settings.METRICS_PORT_INGESTOR, settings.METRICS_ENABLED)

    symbols = settings.ACTIVE_SYMBOLS
    
    collectors = []
    if "binance" in settings.EXCHANGES:
        collectors.append(BinanceCollector(symbols, handle_trade, handle_bbo))
    if "kraken" in settings.EXCHANGES:
        collectors.append(KrakenCollector(symbols, handle_trade, handle_bbo))
    if "coinbase" in settings.EXCHANGES:
        collectors.append(CoinbaseCollector(symbols, handle_trade, handle_bbo))
        
    tasks = [asyncio.create_task(db_writer_loop())]
    for c in collectors:
        tasks.append(asyncio.create_task(c.start()))
        
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Ingestor stopped.")
