import asyncio
import asyncpg
import logging
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from paper_execution.engine import PaperExecutionEngine
from signal_engine.scorer import SignalEngine

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AntigravityBot")

async def get_market_data(pool: asyncpg.Pool, symbol: str):
    async with pool.acquire() as conn:
        bbo = await conn.fetchrow("""
            SELECT bid_px, ask_px, bid_qty, ask_qty, exchange_code
            FROM bbo_tick
            WHERE symbol = $1
            ORDER BY ts_event DESC
            LIMIT 1
        """, symbol)
        
        if not bbo:
            return None
            
        spread_bps = ((bbo['ask_px'] - bbo['bid_px']) / bbo['bid_px']) * 10000
        # Rough estimation of 1% depth using top of book (since we don't have full L2 stored yet)
        depth_1pct_usd = float(bbo['bid_px'] * bbo['bid_qty']) * 10 # heuristic multiplier
        
        return {
            "price": float(bbo['ask_px']), # buy at ask
            "bid_price": float(bbo['bid_px']), # sell at bid
            "spread_bps": float(spread_bps),
            "depth_1pct_usd": float(depth_1pct_usd),
            "exchange_code": bbo['exchange_code']
        }

async def run_bot():
    logger.info("Starting Antigravity Paper Trading Bot...")
    
    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    execution_engine = PaperExecutionEngine(pool)
    signal_engine = SignalEngine(pool)
    
    # Define thresholds
    BUY_THRESHOLD = 0.65
    SELL_THRESHOLD = 0.15
    REDUCE_THRESHOLD = 0.35
    
    try:
        while True:
            logger.info("=== Starting evaluation cycle ===")
            
            # Update portfolio value using current prices
            current_prices = {}
            for symbol in settings.ACTIVE_SYMBOLS:
                mkt = await get_market_data(pool, symbol)
                if mkt:
                    current_prices[symbol] = mkt['bid_price']
            await execution_engine.update_portfolio_value(current_prices)
            
            portfolio_state = await execution_engine.get_portfolio_state()
            logger.info(f"Portfolio Value: ${portfolio_state['total_value']:.2f} | Cash: ${portfolio_state['current_cash']:.2f}")
            
            for symbol in settings.ACTIVE_SYMBOLS:
                mkt_data = await get_market_data(pool, symbol)
                if not mkt_data:
                    logger.warning(f"No recent market data for {symbol}, skipping.")
                    continue
                    
                score = await signal_engine.evaluate_symbol(symbol, mkt_data['exchange_code'])
                s_total = score['s_total']
                logger.info(f"[{symbol}] S_total: {s_total:.4f} (Soc: {score['s_social']:.2f}, Mkt: {score['s_market']:.2f})")
                
                # Check current position
                pos = next((p for p in portfolio_state['positions'] if p['symbol'] == symbol), None)
                
                # Decision Matrix
                if s_total >= BUY_THRESHOLD and not pos:
                    # Target 10% of portfolio per trade
                    target_notional = portfolio_state['total_value'] * 0.10
                    qty = target_notional / mkt_data['price']
                    
                    logger.info(f"[{symbol}] BUY signal triggered.")
                    await execution_engine.execute_trade(
                        symbol=symbol,
                        exchange_code=mkt_data['exchange_code'],
                        side='buy',
                        qty=qty,
                        price=mkt_data['price'],
                        spread_bps=mkt_data['spread_bps'],
                        depth_1pct_usd=mkt_data['depth_1pct_usd'],
                        signal_score=s_total,
                        reason="S_total >= BUY_THRESHOLD"
                    )
                
                elif s_total < SELL_THRESHOLD and pos:
                    logger.info(f"[{symbol}] SELL signal triggered (Full exit).")
                    await execution_engine.execute_trade(
                        symbol=symbol,
                        exchange_code=mkt_data['exchange_code'],
                        side='sell',
                        qty=pos['qty'],
                        price=mkt_data['bid_price'],
                        spread_bps=mkt_data['spread_bps'],
                        depth_1pct_usd=mkt_data['depth_1pct_usd'],
                        signal_score=s_total,
                        reason="S_total < SELL_THRESHOLD"
                    )
            
            # Wait before next cycle
            await asyncio.sleep(15)
            
    except asyncio.CancelledError:
        logger.info("Bot stopped.")
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(run_bot())
