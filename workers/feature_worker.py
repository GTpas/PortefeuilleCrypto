"""
Feature Worker
--------------
Periodically computes market features and writes them to market_feature_1s.
Also snapshots portfolio_state at regular intervals.
"""

import asyncio
import asyncpg
import logging
import json
import sys
import os
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from signal_engine.market_features import MarketFeaturesCalculator
from metrics import (
    start_metrics_server, rows_written_total, worker_last_success_ts,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FeatureWorker")


async def snapshot_portfolio_state(pool: asyncpg.Pool):
    """Write a snapshot of the current portfolio state to portfolio_state hypertable."""
    try:
        async with pool.acquire() as conn:
            portfolio = await conn.fetchrow("""
                SELECT id, initial_capital, current_cash, total_value
                FROM paper_portfolio
                WHERE name = 'Antigravity Default'
            """)
            if not portfolio:
                return

            positions = await conn.fetch("""
                SELECT symbol, exchange_code, qty, average_entry_price, unrealized_pnl
                FROM paper_position
                WHERE portfolio_id = $1 AND qty > 0
            """, portfolio['id'])

            total_value = float(portfolio['total_value'])
            current_cash = float(portfolio['current_cash'])
            initial_capital = float(portfolio['initial_capital'])
            invested_value = total_value - current_cash
            num_positions = len(positions)

            # Max position weight
            max_weight = 0.0
            positions_data = []
            for p in positions:
                qty = float(p['qty'])
                avg_price = float(p['average_entry_price'])
                pos_value = qty * avg_price
                weight = pos_value / total_value if total_value > 0 else 0
                max_weight = max(max_weight, weight)
                positions_data.append({
                    "symbol": p['symbol'],
                    "qty": qty,
                    "avg_price": avg_price,
                    "value": pos_value,
                    "weight": round(weight, 4),
                    "pnl": float(p['unrealized_pnl'])
                })

            drawdown_pct = min(0, (total_value - initial_capital) / initial_capital * 100) if initial_capital > 0 else 0
            exposure_pct = (invested_value / total_value * 100) if total_value > 0 else 0

            await conn.execute("""
                INSERT INTO portfolio_state (
                    ts, portfolio_id, total_value, current_cash, invested_value,
                    num_positions, max_position_weight, drawdown_pct, exposure_pct,
                    positions_snapshot
                ) VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
                portfolio['id'], total_value, current_cash, invested_value,
                num_positions, round(max_weight, 4),
                round(drawdown_pct, 4), round(exposure_pct, 4),
                json.dumps(positions_data)
            )
    except Exception as e:
        logger.error(f"Portfolio snapshot error: {e}")


async def run_feature_worker():
    logger.info("Starting Feature Worker...")

    start_metrics_server(settings.METRICS_PORT_FEATURE, settings.METRICS_ENABLED)
    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    calculator = MarketFeaturesCalculator(pool)

    feature_cycle = 0

    try:
        while True:
            feature_cycle += 1

            for symbol in settings.ACTIVE_SYMBOLS:
                for exchange_code in settings.EXCHANGES:
                    try:
                        features = await calculator.compute_features(symbol, exchange_code)
                        if features:
                            await calculator.write_features(features)
                            rows_written_total.labels(table="market_feature_1s").inc()
                    except Exception as e:
                        logger.error(f"Feature computation error for {symbol}/{exchange_code}: {e}")

            worker_last_success_ts.labels(worker="feature_worker").set(time.time())

            # Snapshot portfolio every 30 cycles (~30s if 1s loop)
            if feature_cycle % 30 == 0:
                await snapshot_portfolio_state(pool)
                logger.debug("Portfolio state snapshot written.")

            if feature_cycle % 60 == 0:
                logger.info(f"Feature worker cycle {feature_cycle} — features computed for {len(settings.ACTIVE_SYMBOLS)} symbols")

            await asyncio.sleep(1)

    except asyncio.CancelledError:
        logger.info("Feature worker stopped.")
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_feature_worker())
    except KeyboardInterrupt:
        logger.info("Feature worker stopped by user.")
