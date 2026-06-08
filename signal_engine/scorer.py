"""
Signal Engine — Scorer
----------------------
Evaluates a symbol and computes the composite score S_total.
Now uses real MarketFeaturesCalculator, SocialEngine, and RiskEngine
instead of random mock values.

S_total = 0.45 * S_social + 0.45 * S_market + 0.10 * (2 * S_risk - 1)
"""

import asyncpg
from typing import Dict, Any, List
import logging
import json
from datetime import datetime, timezone

from signal_engine.market_features import MarketFeaturesCalculator
from signal_engine.risk_engine import RiskEngine
from signal_engine.social_engine import SocialEngine

logger = logging.getLogger(__name__)

# ── Decision thresholds (S_total ∈ [-1, +1], symmetric around 0) ──
# A neutral score (≈0) MUST map to HOLD. The previous mapping exited below
# 0.15, which liquidated positions on absence of signal (PR1 fix).
REINFORCE_THRESHOLD = 0.60   # strongly positive + existing position → add
BUY_THRESHOLD = 0.30         # positive → open / buy
REDUCE_THRESHOLD = -0.30     # negative → trim
EXIT_THRESHOLD = -0.60       # strongly negative → close


class SignalEngine:
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        self.market_calc = MarketFeaturesCalculator(db_pool)
        self.risk_engine = RiskEngine(db_pool)
        self.social_engine = SocialEngine(db_pool)

    async def calculate_market_score(self, symbol: str, exchange_code: str) -> Dict[str, Any]:
        """
        Calculates a market score combining momentum, microstructure, and volume.
        Returns score ∈ [-1, +1] and contributing factors.
        """
        async with self.db_pool.acquire() as conn:
            # Multi-timeframe returns
            returns = await self.market_calc.compute_returns(conn, symbol, exchange_code)

            # Latest market features
            features = await self.market_calc.compute_features(symbol, exchange_code)

            if not features and not any(returns.values()):
                return {
                    "score": 0.0,
                    "factors": [
                        {"name": "insufficient_data", "value": 0.0, "contrib": 0.0,
                         "explanation": "Not enough market data for scoring"}
                    ],
                    "features": None,
                }

            factors = []
            score_components = []

            # ── Momentum (multi-timeframe) ──
            ret_15m = returns.get("ret_15m", 0.0)
            ret_1h = returns.get("ret_1h", 0.0)
            ret_4h = returns.get("ret_4h", 0.0)

            # Normalize returns: 5% in 15m is extreme
            momentum_15m = max(-1.0, min(1.0, ret_15m / 0.05))
            momentum_1h = max(-1.0, min(1.0, ret_1h / 0.10))
            momentum_4h = max(-1.0, min(1.0, ret_4h / 0.15))

            # Trend alignment: all timeframes agree = stronger signal
            trend_alignment = 0.0
            if (momentum_15m > 0 and momentum_1h > 0 and momentum_4h > 0):
                trend_alignment = min(1.0, (momentum_15m + momentum_1h + momentum_4h) / 3)
            elif (momentum_15m < 0 and momentum_1h < 0 and momentum_4h < 0):
                trend_alignment = max(-1.0, (momentum_15m + momentum_1h + momentum_4h) / 3)

            factors.append({"name": "ret_15m", "value": ret_15m, "contrib": momentum_15m * 0.3,
                           "explanation": f"15m return: {ret_15m*100:+.2f}%"})
            factors.append({"name": "ret_1h", "value": ret_1h, "contrib": momentum_1h * 0.25,
                           "explanation": f"1h return: {ret_1h*100:+.2f}%"})
            factors.append({"name": "trend_alignment", "value": trend_alignment, "contrib": trend_alignment * 0.15,
                           "explanation": f"Trend alignment across timeframes: {trend_alignment:+.2f}"})

            score_components.append(momentum_15m * 0.30)
            score_components.append(momentum_1h * 0.25)
            score_components.append(trend_alignment * 0.15)

            # ── Microstructure ──
            if features:
                # Book imbalance: positive = more bids (bullish)
                imbalance = features.get('book_imbalance', 0)
                factors.append({"name": "book_imbalance", "value": imbalance, "contrib": imbalance * 0.10,
                               "explanation": f"Order book imbalance: {imbalance:+.3f} ({'buy pressure' if imbalance > 0.1 else 'sell pressure' if imbalance < -0.1 else 'balanced'})"})
                score_components.append(imbalance * 0.10)

                # Trade pressure
                pressure = features.get('trade_pressure', 0)
                factors.append({"name": "trade_pressure", "value": pressure, "contrib": pressure * 0.10,
                               "explanation": f"Trade flow pressure: {pressure:+.3f}"})
                score_components.append(pressure * 0.10)

                # Relative volume
                rel_vol = features.get('relative_volume', 1.0)
                vol_signal = max(-1.0, min(1.0, (rel_vol - 1.0) / 2.0))  # >1 = above avg
                factors.append({"name": "relative_volume", "value": rel_vol, "contrib": vol_signal * 0.10,
                               "explanation": f"Volume {rel_vol:.1f}x vs 24h average"})
                score_components.append(vol_signal * 0.10)
            else:
                factors.append({"name": "microstructure", "value": 0.0, "contrib": 0.0,
                               "explanation": "No microstructure data available"})

            total_score = sum(score_components)
            total_score = max(-1.0, min(1.0, total_score))

            return {
                "score": round(total_score, 4),
                "factors": factors,
                "features": features,
            }

    async def evaluate_symbol(self, symbol: str, exchange_code: str,
                               portfolio_state: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Evaluates a symbol and computes the composite score S_total.
        Returns the individual scores, factors, and the final S_total.
        Writes the snapshot and factors to the database.
        """
        # 1. Market Score
        market_res = await self.calculate_market_score(symbol, exchange_code)
        s_market = market_res["score"]
        market_factors = market_res["factors"]
        market_features = market_res.get("features")

        # 2. Social Score (real, from SocialEngine).
        # social_quality ∈ {"real", "unavailable", "fallback"}:
        #   real        → computed from genuine social content
        #   unavailable → no real feed configured (neutral 0.0, NOT presented as real)
        #   fallback    → engine error (neutral 0.0)
        missing_features: List[str] = []
        try:
            social_res = await self.social_engine.compute_social_score(symbol)
            if social_res.get("available"):
                s_social = social_res["score"]
                social_factors = social_res["factors"]
                social_quality = "real"
            else:
                # No real social feed: neutral contribution, explicitly flagged.
                s_social = 0.0
                social_factors = social_res.get("factors") or [
                    {"name": "social_unavailable", "value": 0.0, "contrib": 0.0,
                     "explanation": "No real social feed configured — social signal unavailable"}
                ]
                social_quality = "unavailable"
                missing_features.append("s_social")
        except Exception as e:
            logger.warning(f"Social engine failed for {symbol}, falling back to neutral: {e}")
            s_social = 0.0
            social_factors = [
                {"name": "social_unavailable", "value": 0.0, "contrib": 0.0,
                 "explanation": "Social engine error — neutral fallback"}
            ]
            social_quality = "fallback"
            missing_features.append("s_social")

        # 3. Risk Score (real, from RiskEngine)
        if portfolio_state is None:
            portfolio_state = await self._get_portfolio_state()

        # Get realized volatility
        async with self.db_pool.acquire() as conn:
            realized_vol = await self.market_calc.compute_realized_volatility(
                conn, symbol, exchange_code
            )

        risk_res = await self.risk_engine.evaluate(
            symbol, exchange_code, portfolio_state,
            market_features=market_features,
            realized_vol=realized_vol,
        )
        s_risk = risk_res["score"]
        risk_factors = risk_res["factors"]
        risk_gates = risk_res["gates"]

        # S_total calculation
        s_total = (0.45 * s_social) + (0.45 * s_market) + (0.10 * (2 * s_risk - 1))
        s_total = round(max(-1.0, min(1.0, s_total)), 4)

        # Determine proposed action. Risk gates always force HOLD.
        action_proposed, reason_code = self._decide_action(s_total, risk_gates)

        # Data freshness (for observability + audit). market_features carries the
        # age of the latest BBO used; social age comes from the social engine.
        market_data_age_ms = int(market_features.get("data_age_ms", 0)) if market_features else 0
        social_data_age_ms = 0
        if social_quality == "real":
            social_data_age_ms = int(social_res.get("metrics", {}).get("data_age_ms", 0))

        # Compute confidence score based on data quality
        confidence_score = self._compute_confidence(
            social_quality, market_features is not None,
            len(market_factors), len(social_factors), len(risk_factors)
        )

        # Determine quality grade
        quality_grade = "full"
        degradation_reasons = []
        if social_quality in ("unavailable", "fallback"):
            quality_grade = "partial"
            degradation_reasons.append("social_data_unavailable")
        if market_features is None:
            quality_grade = "degraded"
            degradation_reasons.append("market_features_unavailable")
            missing_features.append("s_market")

        # Honest data-quality summary surfaced to the API/UI so the cockpit can
        # show which sub-scores are real vs neutral-because-missing.
        data_quality = {
            "social": social_quality,          # real | unavailable | fallback
            "market": "real" if market_features is not None else "unavailable",
            "grade": quality_grade,             # full | partial | degraded
            "social_available": social_quality == "real",
            "market_available": market_features is not None,
            "missing_features": missing_features,
        }

        # Log to database for traceability
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                # Insert snapshot
                snapshot_id = await conn.fetchval("""
                    INSERT INTO decision_snapshot
                    (symbol, exchange_code, s_social, s_market, s_risk, s_total,
                     action_proposed, confidence_score, reason_code, quality_grade)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING id
                """, symbol, exchange_code,
                    float(s_social), float(s_market), float(s_risk), float(s_total),
                    action_proposed, float(confidence_score), reason_code, quality_grade)

                # Insert all factors
                for f in market_factors:
                    await conn.execute("""
                        INSERT INTO decision_factor
                        (decision_snapshot_id, factor_category, factor_name, factor_value, score_contribution, explanation)
                        VALUES ($1, 'market', $2, $3, $4, $5)
                    """, snapshot_id, f['name'], float(f['value']), float(f['contrib']), f['explanation'])

                for f in social_factors:
                    await conn.execute("""
                        INSERT INTO decision_factor
                        (decision_snapshot_id, factor_category, factor_name, factor_value, score_contribution, explanation)
                        VALUES ($1, 'social', $2, $3, $4, $5)
                    """, snapshot_id, f['name'], float(f['value']), float(f['contrib']), f['explanation'])

                for f in risk_factors:
                    await conn.execute("""
                        INSERT INTO decision_factor
                        (decision_snapshot_id, factor_category, factor_name, factor_value, score_contribution, explanation)
                        VALUES ($1, 'risk', $2, $3, $4, $5)
                    """, snapshot_id, f['name'], float(f['value']), float(f['contrib']), f['explanation'])

                # Write signal quality audit
                await conn.execute("""
                    INSERT INTO signal_quality_audit
                    (decision_snapshot_id, symbol,
                     social_sources_count, market_data_age_ms, social_data_age_ms,
                     has_sufficient_social, has_sufficient_market,
                     quality_grade, degradation_reasons)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                    snapshot_id, symbol,
                    len(social_factors), market_data_age_ms, social_data_age_ms,
                    social_quality == "real", market_features is not None,
                    quality_grade, degradation_reasons
                )

        return {
            "symbol": symbol,
            "exchange_code": exchange_code,
            "s_social": s_social,
            "s_market": s_market,
            "s_risk": s_risk,
            "s_total": s_total,
            "action_proposed": action_proposed,
            "reason_code": reason_code,
            "confidence_score": confidence_score,
            "quality_grade": quality_grade,
            "data_quality": data_quality,
            "missing_features": missing_features,
            "social_available": social_quality == "real",
            "risk_gates": risk_gates,
            "snapshot_id": snapshot_id,
            "tradeable": risk_res["tradeable"],
            # Single source of truth for downstream execution (spread/depth).
            "features": market_features,
        }

    @staticmethod
    def _decide_action(s_total: float, risk_gates: List[str]) -> tuple:
        """
        Map S_total ∈ [-1, +1] to an action, with thresholds symmetric around 0.
        A neutral score maps to HOLD; any triggered risk gate forces HOLD.
        """
        if risk_gates:
            return "hold", f"risk_gate:{risk_gates[0]}"
        if s_total >= REINFORCE_THRESHOLD:
            return "reinforce", "s_total_reinforce"
        if s_total >= BUY_THRESHOLD:
            return "buy", "s_total_buy"
        if s_total <= EXIT_THRESHOLD:
            return "exit", "s_total_exit"
        if s_total <= REDUCE_THRESHOLD:
            return "reduce", "s_total_reduce"
        return "hold", "hold_neutral"

    async def _get_portfolio_state(self) -> Dict[str, Any]:
        """Fetch current portfolio state from DB."""
        async with self.db_pool.acquire() as conn:
            portfolio = await conn.fetchrow("""
                SELECT id, initial_capital, current_cash, total_value
                FROM paper_portfolio WHERE name = 'Antigravity Default'
            """)
            if not portfolio:
                return {"total_value": 10000, "current_cash": 10000, "initial_capital": 10000, "positions": []}

            positions = await conn.fetch("""
                SELECT symbol, exchange_code, qty, average_entry_price, unrealized_pnl
                FROM paper_position WHERE portfolio_id = $1 AND qty > 0
            """, portfolio['id'])

            return {
                "id": portfolio['id'],
                "initial_capital": float(portfolio['initial_capital']),
                "current_cash": float(portfolio['current_cash']),
                "total_value": float(portfolio['total_value']),
                "positions": [dict(p) for p in positions],
            }

    @staticmethod
    def _compute_confidence(social_quality: str, has_market: bool,
                            market_factor_count: int, social_factor_count: int,
                            risk_factor_count: int) -> float:
        """
        Compute decision confidence based on data completeness.
        1.0 = all data sources available and rich, 0.0 = severely degraded.
        """
        score = 0.0

        # Social data quality
        if social_quality == "real":
            score += 0.35
        elif social_quality == "fallback":
            score += 0.05

        # Market data quality
        if has_market:
            score += 0.35
        else:
            score += 0.10

        # Factor richness
        total_factors = market_factor_count + social_factor_count + risk_factor_count
        if total_factors >= 10:
            score += 0.30
        elif total_factors >= 5:
            score += 0.20
        else:
            score += 0.05

        return round(min(1.0, score), 2)
