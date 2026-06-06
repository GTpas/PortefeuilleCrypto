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

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), "INFO"))
logger = logging.getLogger(__name__)

trade_queue: asyncio.Queue = asyncio.Queue(maxsize=50000)
bbo_queue: asyncio.Queue = asyncio.Queue(maxsize=50000)

def handle_trade(trade: TradeTick):
    try:
        trade_queue.put_nowait(trade)
    except asyncio.QueueFull:
        logger.warning("Trade queue full, dropping event")

def handle_bbo(bbo: BBOTick):
    try:
        bbo_queue.put_nowait(bbo)
    except asyncio.QueueFull:
        logger.warning("BBO queue full, dropping event")

def sync_db_write(trades: List[TradeTick], bbos: List[BBOTick]):
    # Note: creates a new connection per batch or reuses.
    # It's better to instantiate it in the thread or keep it around.
    # We will instantiate writer per thread execution for safety, or keep a thread-local one.
    writer = DatabaseWriter()
    try:
        writer.write_batch(trades, bbos)
    finally:
        writer.close()

async def db_writer_loop():
    logger.info("Starting DB writer loop...")
    last_flush = time.time()
    trades_buffer = []
    bbos_buffer = []

    while True:
        now = time.time()
        
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
