import asyncio
import asyncpg
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class PaperExecutionEngine:
    def __init__(self, db_pool: asyncpg.Pool, portfolio_name: str = 'Antigravity Default'):
        self.db_pool = db_pool
        self.portfolio_name = portfolio_name
        self.max_positions = 8
        self.max_weight_per_position = 0.20
        self.min_cash_ratio = 0.10
        self.fees_bps = 10

    async def get_portfolio_state(self) -> Dict[str, Any]:
        async with self.db_pool.acquire() as conn:
            portfolio = await conn.fetchrow("""
                SELECT id, initial_capital, current_cash, total_value
                FROM paper_portfolio
                WHERE name = $1
            """, self.portfolio_name)
            
            if not portfolio:
                raise ValueError(f"Portfolio {self.portfolio_name} not found")

            positions = await conn.fetch("""
                SELECT symbol, exchange_code, qty, average_entry_price, unrealized_pnl
                FROM paper_position
                WHERE portfolio_id = $1 AND qty > 0
            """, portfolio['id'])

            return {
                "id": portfolio['id'],
                "initial_capital": float(portfolio['initial_capital']),
                "current_cash": float(portfolio['current_cash']),
                "total_value": float(portfolio['total_value']),
                # Normalize NUMERIC columns (asyncpg returns Decimal) to float at the
                # boundary so downstream arithmetic never mixes float/Decimal.
                "positions": [
                    {
                        **dict(p),
                        "qty": float(p['qty']),
                        "average_entry_price": float(p['average_entry_price']),
                        "unrealized_pnl": float(p['unrealized_pnl']) if p['unrealized_pnl'] is not None else 0.0,
                    }
                    for p in positions
                ]
            }

    async def execute_trade(self, symbol: str, exchange_code: str, side: str, 
                          qty: float, price: float, spread_bps: float, 
                          depth_1pct_usd: float, signal_score: float, reason: str,
                          snapshot_id: Optional[int] = None) -> bool:
        """
        Executes a simulated trade applying Antigravity risk rules and dynamic slippage.
        Returns True if executed, False if rejected by risk engine.
        """
        if side not in ['buy', 'sell']:
            raise ValueError("Side must be 'buy' or 'sell'")

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                state = await self.get_portfolio_state()
                portfolio_id = state['id']
                total_value = state['total_value']
                current_cash = state['current_cash']
                
                # Check 1: Max 8 positions (for buy)
                if side == 'buy':
                    num_positions = len(state['positions'])
                    has_position = any(p['symbol'] == symbol and p['exchange_code'] == exchange_code for p in state['positions'])
                    if not has_position and num_positions >= self.max_positions:
                        logger.warning(f"Trade rejected: Max {self.max_positions} positions reached.")
                        return False
                
                # Calculate dynamic slippage
                # slippage_bps = max(5, spread_bps + impact_coeff * order_notional / depth_1pct_usd)
                impact_coeff = 0.5
                order_notional = qty * price
                if depth_1pct_usd <= 0:
                    logger.warning("Trade rejected: Insufficient order book depth data.")
                    return False
                    
                slippage_bps = max(5.0, spread_bps + impact_coeff * (order_notional / depth_1pct_usd))
                if slippage_bps > 40.0:
                    logger.warning(f"Trade rejected: Expected slippage {slippage_bps:.2f} bps exceeds 40 bps limit.")
                    return False
                
                # Calculate final execution price and fees
                slippage_factor = (1 + slippage_bps/10000) if side == 'buy' else (1 - slippage_bps/10000)
                exec_price = price * slippage_factor
                fees = (qty * exec_price) * (self.fees_bps / 10000)
                total_cost = (qty * exec_price) + fees if side == 'buy' else (qty * exec_price) - fees
                
                if side == 'buy':
                    # Check 2: Max 20% exposure per position
                    current_exposure = sum(p['qty'] * price for p in state['positions'] if p['symbol'] == symbol)
                    new_exposure = current_exposure + (qty * price)
                    if new_exposure / total_value > self.max_weight_per_position:
                        logger.warning(f"Trade rejected: Position would exceed {self.max_weight_per_position*100}% of portfolio.")
                        return False
                        
                    # Check 3: Insufficient cash
                    if current_cash - total_cost < (total_value * self.min_cash_ratio):
                        logger.warning("Trade rejected: Insufficient cash or violates min 10% cash rule.")
                        return False
                else:
                    # Sell check
                    pos = next((p for p in state['positions'] if p['symbol'] == symbol), None)
                    if not pos or pos['qty'] < qty:
                        logger.warning("Trade rejected: Insufficient position to sell.")
                        return False

                # Execute order
                await conn.execute("""
                    INSERT INTO paper_trade (portfolio_id, symbol, exchange_code, side, qty, price, slippage_bps, fees, signal_score, reason, decision_snapshot_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """, portfolio_id, symbol, exchange_code, side, float(qty), float(exec_price), float(slippage_bps), float(fees), float(signal_score), reason, snapshot_id)

                
                # Update Cash
                new_cash = current_cash - total_cost if side == 'buy' else current_cash + total_cost
                await conn.execute("UPDATE paper_portfolio SET current_cash = $1, updated_at = now() WHERE id = $2", float(new_cash), portfolio_id)
                
                # Update Position
                pos_record = await conn.fetchrow("SELECT qty, average_entry_price FROM paper_position WHERE portfolio_id = $1 AND symbol = $2", portfolio_id, symbol)
                if not pos_record:
                    await conn.execute("""
                        INSERT INTO paper_position (portfolio_id, symbol, exchange_code, qty, average_entry_price)
                        VALUES ($1, $2, $3, $4, $5)
                    """, portfolio_id, symbol, exchange_code, float(qty), float(exec_price))
                else:
                    old_qty = float(pos_record['qty'])
                    old_avg_price = float(pos_record['average_entry_price'])
                    
                    if side == 'buy':
                        new_qty = old_qty + qty
                        new_avg_price = ((old_qty * old_avg_price) + (qty * exec_price)) / new_qty
                    else:
                        new_qty = old_qty - qty
                        new_avg_price = old_avg_price if new_qty > 0 else 0.0
                        
                    await conn.execute("""
                        UPDATE paper_position 
                        SET qty = $1, average_entry_price = $2, updated_at = now() 
                        WHERE portfolio_id = $3 AND symbol = $4
                    """, float(new_qty), float(new_avg_price), portfolio_id, symbol)
                
                logger.info(f"Simulated {side.upper()} {qty} {symbol} at {exec_price:.4f} (slippage: {slippage_bps:.1f} bps)")
                return True

    async def update_portfolio_value(self, current_prices: Dict[str, float]):
        """Updates the total portfolio value and unrealized PnL based on current market prices."""
        async with self.db_pool.acquire() as conn:
            state = await self.get_portfolio_state()
            total_value = state['current_cash']
            
            for pos in state['positions']:
                symbol = pos['symbol']
                if symbol in current_prices:
                    price = current_prices[symbol]
                    qty = pos['qty']
                    unrealized_pnl = (price - pos['average_entry_price']) * qty
                    total_value += (qty * price)
                    
                    await conn.execute("""
                        UPDATE paper_position 
                        SET unrealized_pnl = $1, updated_at = now() 
                        WHERE portfolio_id = $2 AND symbol = $3
                    """, float(unrealized_pnl), state['id'], symbol)
            
            await conn.execute("""
                UPDATE paper_portfolio 
                SET total_value = $1, updated_at = now() 
                WHERE id = $2
            """, float(total_value), state['id'])
