import asyncpg
from typing import Dict, Any, List
import logging
import random
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class SignalEngine:
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool

    async def calculate_market_score(self, symbol: str) -> Dict[str, Any]:
        """
        Calculates a market momentum score and returns the score along with contributing factors.
        """
        async with self.db_pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT bucket_start, close, volume_base, trade_count
                FROM ohlcv_1s 
                WHERE symbol = $1 
                ORDER BY bucket_start DESC 
                LIMIT 900
            """, symbol)
            
            if len(records) < 2:
                return {
                    "score": 0.0,
                    "factors": [
                        {"name": "insufficient_data", "value": 0.0, "contrib": 0.0, "explanation": "Not enough historical data"}
                    ]
                }
                
            current_price = float(records[0]['close'])
            oldest_price = float(records[-1]['close'])
            
            # Simple momentum: % change
            change = (current_price - oldest_price) / oldest_price
            
            # Normalize change to [-1, 1] (Assume 5% move in 15m is extreme)
            momentum_score = max(-1.0, min(1.0, change / 0.05))
            
            # Mock volume relative score (could be computed from 24h average)
            rel_vol_score = random.uniform(-0.2, 0.2)
            
            total_score = max(-1.0, min(1.0, momentum_score + rel_vol_score))
            
            return {
                "score": total_score,
                "factors": [
                    {"name": "momentum_15m", "value": change, "contrib": momentum_score, "explanation": f"15m price change of {change*100:.2f}%"},
                    {"name": "relative_volume", "value": 1.5, "contrib": rel_vol_score, "explanation": "Volume is elevated compared to recent average"}
                ]
            }

    async def evaluate_symbol(self, symbol: str, exchange_code: str) -> Dict[str, Any]:
        """
        Evaluates a symbol and computes the composite score S_total.
        Returns the individual scores, factors, and the final S_total.
        Writes the snapshot and factors to the database.
        """
        # 1. Market Score
        market_res = await self.calculate_market_score(symbol)
        s_market = market_res["score"]
        market_factors = market_res["factors"]
        
        # 2. Social Score (Mocked for now)
        s_social = random.uniform(-0.5, 0.5)
        mention_velocity_z = random.uniform(0.0, 4.0)
        bot_risk = random.uniform(0.0, 0.2)
        social_factors = [
            {"name": "mention_velocity_z", "value": mention_velocity_z, "contrib": s_social + bot_risk, "explanation": f"Mentions are {mention_velocity_z:.1f} standard deviations above mean"},
            {"name": "bot_risk_penalty", "value": bot_risk, "contrib": -bot_risk, "explanation": f"Penalty for detected bot-like activity"}
        ]
        
        # 3. Risk Score (Mocked. 1.0 = extremely safe, 0.0 = extreme risk)
        s_risk = random.uniform(0.5, 1.0)
        spread_bps = random.uniform(1.0, 15.0)
        risk_factors = [
            {"name": "spread_bps", "value": spread_bps, "contrib": s_risk, "explanation": f"Current spread is {spread_bps:.1f} bps"}
        ]
        
        # S_total calculation
        s_total = (0.45 * s_social) + (0.45 * s_market) + (0.10 * (2 * s_risk - 1))
        
        # Determine action proposed
        action_proposed = "hold"
        if s_total >= 0.65:
            action_proposed = "buy"
        elif s_total < 0.15:
            action_proposed = "exit"
        elif s_total < 0.35:
            action_proposed = "reduce"

        # Log to database for traceability
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                # Insert snapshot
                snapshot_id = await conn.fetchval("""
                    INSERT INTO decision_snapshot 
                    (symbol, exchange_code, s_social, s_market, s_risk, s_total, action_proposed, confidence_score)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING id
                """, symbol, exchange_code, float(s_social), float(s_market), float(s_risk), float(s_total), action_proposed, 0.85)
                
                # Insert factors
                for f in market_factors:
                    await conn.execute("""
                        INSERT INTO decision_factor (decision_snapshot_id, factor_category, factor_name, factor_value, score_contribution, explanation)
                        VALUES ($1, 'market', $2, $3, $4, $5)
                    """, snapshot_id, f['name'], float(f['value']), float(f['contrib']), f['explanation'])
                    
                for f in social_factors:
                    await conn.execute("""
                        INSERT INTO decision_factor (decision_snapshot_id, factor_category, factor_name, factor_value, score_contribution, explanation)
                        VALUES ($1, 'social', $2, $3, $4, $5)
                    """, snapshot_id, f['name'], float(f['value']), float(f['contrib']), f['explanation'])
                    
                for f in risk_factors:
                    await conn.execute("""
                        INSERT INTO decision_factor (decision_snapshot_id, factor_category, factor_name, factor_value, score_contribution, explanation)
                        VALUES ($1, 'risk', $2, $3, $4, $5)
                    """, snapshot_id, f['name'], float(f['value']), float(f['contrib']), f['explanation'])

        return {
            "symbol": symbol,
            "s_social": s_social,
            "s_market": s_market,
            "s_risk": s_risk,
            "s_total": s_total,
            "action_proposed": action_proposed,
            "snapshot_id": snapshot_id
        }
