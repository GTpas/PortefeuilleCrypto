import asyncpg
from typing import Dict, Any, List
import logging
import random

logger = logging.getLogger(__name__)

class SignalEngine:
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool

    async def calculate_market_score(self, symbol: str) -> float:
        """
        Calculates a simple market momentum score [-1, +1] based on recent price action.
        Uses the last 15 minutes of ohlcv_1s data.
        """
        async with self.db_pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT close 
                FROM ohlcv_1s 
                WHERE symbol = $1 
                ORDER BY bucket_start DESC 
                LIMIT 900
            """, symbol)
            
            if len(records) < 2:
                return 0.0
                
            current_price = float(records[0]['close'])
            oldest_price = float(records[-1]['close'])
            
            # Simple momentum: % change
            change = (current_price - oldest_price) / oldest_price
            
            # Normalize change to [-1, 1] (Assume 5% move in 15m is extreme)
            score = max(-1.0, min(1.0, change / 0.05))
            return score

    async def evaluate_symbol(self, symbol: str, exchange_code: str) -> Dict[str, Any]:
        """
        Evaluates a symbol and computes the composite score S_total.
        Returns the individual scores and the final S_total.
        """
        # 1. Market Score (computed from DB)
        s_market = await self.calculate_market_score(symbol)
        
        # 2. Social Score (Mocked for now until social API ingestors are built)
        # Random walk around 0 for simulation realism
        s_social = random.uniform(-0.5, 0.5) 
        
        # 3. Risk Score (Mocked. 1.0 = extremely safe, 0.0 = extreme risk)
        s_risk = random.uniform(0.5, 1.0)
        
        # S_total calculation
        s_total = (0.45 * s_social) + (0.45 * s_market) + (0.10 * (2 * s_risk - 1))
        
        # Log to database for traceability
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO signal_log (symbol, exchange_code, s_social, s_market, s_risk, s_total, details)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """, symbol, exchange_code, float(s_social), float(s_market), float(s_risk), float(s_total), '{"status": "simulated_social"}')
            
        return {
            "symbol": symbol,
            "s_social": s_social,
            "s_market": s_market,
            "s_risk": s_risk,
            "s_total": s_total
        }
