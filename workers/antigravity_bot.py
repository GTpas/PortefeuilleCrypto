"""
Antigravity Paper Trading Bot
------------------------------
Evaluates all active symbols periodically using the real SignalEngine,
RiskEngine, and SocialEngine. Executes paper trades based on S_total
thresholds with full decision traceability.

Decision matrix (action + thresholds are owned by SignalEngine, see
signal_engine/scorer.py — S_total ∈ [-1, +1], symmetric around 0):
- reinforce + existing position → add
- buy + no position            → open
- reduce + has position        → sell 50%
- exit + has position          → sell all
- risk gate triggered / neutral → hold
"""

import asyncio
import asyncpg
import json
import logging
import sys
import os
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from paper_execution.engine import PaperExecutionEngine
from signal_engine.scorer import SignalEngine
from metrics import (
    start_metrics_server, ai_decisions_total, paper_orders_total,
    worker_last_success_ts,
)

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

        # Live execution prices + exchange selection only.
        # Spread and depth are NOT computed here anymore — they come from the
        # SignalEngine features (market_feature_1s) so there is a single source
        # of truth for microstructure across scoring and execution.
        spread_bps = ((bbo['ask_px'] - bbo['bid_px']) / bbo['bid_px']) * 10000

        return {
            "price": float(bbo['ask_px']),
            "bid_price": float(bbo['bid_px']),
            "spread_bps": float(spread_bps),  # fallback only
            "exchange_code": bbo['exchange_code']
        }


async def write_evidence_links(pool: asyncpg.Pool, snapshot_id: int, symbol: str):
    """Link the most relevant recent social content to this decision."""
    try:
        base_asset = symbol.split('/')[0] if '/' in symbol else symbol
        async with pool.acquire() as conn:
            # Find the most recent and relevant content for this asset
            evidence = await conn.fetch("""
                SELECT rc.id, ce.entity_confidence
                FROM content_entity ce
                JOIN raw_content rc ON rc.id = ce.raw_content_id
                WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
                  AND rc.published_at >= now() - interval '10 minutes'
                ORDER BY rc.published_at DESC
                LIMIT 10
            """, base_asset)

            for e in evidence:
                await conn.execute("""
                    INSERT INTO decision_evidence_link
                    (decision_snapshot_id, raw_content_id, relevance_score)
                    VALUES ($1, $2, $3)
                """, snapshot_id, e['id'], float(e['entity_confidence']))

    except Exception as e:
        logger.error(f"Failed to write evidence links: {e}")


async def run_bot():
    logger.info("Starting Antigravity Paper Trading Bot...")

    start_metrics_server(settings.METRICS_PORT_BOT, settings.METRICS_ENABLED)

    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    execution_engine = PaperExecutionEngine(pool)
    signal_engine = SignalEngine(pool)

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

                # Evaluate with real engines
                score = await signal_engine.evaluate_symbol(
                    symbol, mkt_data['exchange_code'],
                    portfolio_state=portfolio_state
                )
                s_total = score['s_total']
                action = score['action_proposed']
                reason = score['reason_code']
                tradeable = score.get('tradeable', True)
                risk_gates = score.get('risk_gates', [])
                ai_decisions_total.labels(action=action).inc()

                logger.info(
                    f"[{symbol}] S_total: {s_total:.4f} "
                    f"(SOC: {score['s_social']:+.2f}, MKT: {score['s_market']:+.2f}, "
                    f"RSK: {score['s_risk']:.2f}) → {action} "
                    f"[Q: {score['quality_grade']}, C: {score['confidence_score']:.0%}]"
                    f"{' ⛔ GATES: ' + ', '.join(risk_gates) if risk_gates else ''}"
                )

                # Write evidence links for this decision
                await write_evidence_links(pool, score['snapshot_id'], symbol)

                # Unified microstructure for execution: prefer SignalEngine
                # features (single source of truth), fall back to live bbo.
                feats = score.get('features') or {}
                exec_spread_bps = feats.get('spread_bps', mkt_data['spread_bps'])
                exec_depth_usd = feats.get('depth_usd_10bps', 0.0)

                # Check current position
                pos = next((p for p in portfolio_state['positions'] if p['symbol'] == symbol), None)

                # ── Decision Matrix ──

                if not tradeable:
                    logger.info(f"[{symbol}] Trade blocked by risk gates: {risk_gates}")
                    continue

                if action == "buy" and not pos:
                    # Target 10% of portfolio per new position
                    target_notional = portfolio_state['total_value'] * 0.10
                    qty = target_notional / mkt_data['price']

                    logger.info(f"[{symbol}] 🟢 BUY signal — {reason}")
                    executed = await execution_engine.execute_trade(
                        symbol=symbol,
                        exchange_code=mkt_data['exchange_code'],
                        side='buy',
                        qty=qty,
                        price=mkt_data['price'],
                        spread_bps=exec_spread_bps,
                        depth_1pct_usd=exec_depth_usd,
                        signal_score=s_total,
                        reason=f"BUY: {reason}",
                        snapshot_id=score['snapshot_id']
                    )
                    if executed:
                        paper_orders_total.labels(side='buy').inc()

                elif action == "reinforce" and pos:
                    # Reinforce: add 5% of portfolio
                    target_notional = portfolio_state['total_value'] * 0.05
                    qty = target_notional / mkt_data['price']

                    logger.info(f"[{symbol}] 🔵 REINFORCE signal — {reason}")
                    executed = await execution_engine.execute_trade(
                        symbol=symbol,
                        exchange_code=mkt_data['exchange_code'],
                        side='buy',
                        qty=qty,
                        price=mkt_data['price'],
                        spread_bps=exec_spread_bps,
                        depth_1pct_usd=exec_depth_usd,
                        signal_score=s_total,
                        reason=f"REINFORCE: {reason}",
                        snapshot_id=score['snapshot_id']
                    )
                    if executed:
                        paper_orders_total.labels(side='buy').inc()

                elif action == "exit" and pos:
                    # Full exit
                    logger.info(f"[{symbol}] 🔴 EXIT signal — {reason}")
                    executed = await execution_engine.execute_trade(
                        symbol=symbol,
                        exchange_code=mkt_data['exchange_code'],
                        side='sell',
                        qty=float(pos['qty']),
                        price=mkt_data['bid_price'],
                        spread_bps=exec_spread_bps,
                        depth_1pct_usd=exec_depth_usd,
                        signal_score=s_total,
                        reason=f"EXIT: {reason}",
                        snapshot_id=score['snapshot_id']
                    )
                    if executed:
                        paper_orders_total.labels(side='sell').inc()

                elif action == "reduce" and pos:
                    # Reduce 50% of position
                    sell_qty = float(pos['qty']) * 0.5
                    if sell_qty > 0:
                        logger.info(f"[{symbol}] 🟡 REDUCE signal — selling 50% — {reason}")
                        executed = await execution_engine.execute_trade(
                            symbol=symbol,
                            exchange_code=mkt_data['exchange_code'],
                            side='sell',
                            qty=sell_qty,
                            price=mkt_data['bid_price'],
                            spread_bps=exec_spread_bps,
                            depth_1pct_usd=exec_depth_usd,
                            signal_score=s_total,
                            reason=f"REDUCE: {reason}",
                            snapshot_id=score['snapshot_id']
                        )
                        if executed:
                            paper_orders_total.labels(side='sell').inc()

            worker_last_success_ts.labels(worker="antigravity_bot").set(time.time())

            # Wait before next cycle
            await asyncio.sleep(15)

    except asyncio.CancelledError:
        logger.info("Bot stopped.")
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(run_bot())
