"""
Risk Engine
-----------
Computes the real S_risk score based on portfolio concentration,
liquidity, volatility, and correlation analysis.
Replaces the previous random.uniform(0.5, 1.0) mock.
"""

import asyncpg
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from config import settings

logger = logging.getLogger(__name__)


class RiskEngine:
    """
    Evaluates portfolio and market risk to produce S_risk ∈ [0, 1].
    1.0 = extremely safe, 0.0 = extreme risk / no-trade.
    
    Also provides no-trade gates that can block execution
    even when S_social and S_market are strongly positive.
    """

    # Gate thresholds
    MAX_SPREAD_BPS = 15.0
    MAX_SLIPPAGE_BPS = 40.0
    MIN_DEPTH_USD = 500.0
    MAX_POSITION_WEIGHT = 0.20
    MAX_PORTFOLIO_VOL_ANNUALIZED = 1.5  # 150% annualized vol
    MAX_BTC_CORR = 0.95
    MAX_DRAWDOWN_PCT = -15.0  # -15% max drawdown

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool

    async def evaluate(
        self,
        symbol: str,
        exchange_code: str,
        portfolio_state: Dict[str, Any],
        market_features: Optional[Dict[str, Any]] = None,
        realized_vol: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Compute S_risk and risk factors.
        
        Returns:
            {
                "score": float,       # S_risk ∈ [0, 1]
                "gates": list,        # list of triggered no-trade gates
                "factors": list,      # list of factor dicts for decision_factor
                "tradeable": bool,    # whether a trade should be allowed
            }
        """
        factors = []
        gates = []
        risk_scores = []

        # ── 0. Data freshness gate ──────────────
        # Block trading on stale market data: the bot must never act on a quote
        # older than MAX_DATA_AGE_S seconds.
        data_age_ms = market_features.get('data_age_ms', 0) if market_features else None
        if data_age_ms is None:
            gates.append("data_unavailable")
        else:
            max_age_ms = settings.MAX_DATA_AGE_S * 1000
            factors.append({
                "name": "market_data_age_ms",
                "value": data_age_ms,
                "contrib": 0.0,
                "explanation": f"Latest quote is {data_age_ms/1000:.1f}s old (max {settings.MAX_DATA_AGE_S}s)"
            })
            if data_age_ms > max_age_ms:
                gates.append(f"data_stale ({data_age_ms/1000:.1f}s > {settings.MAX_DATA_AGE_S}s)")

        # ── 1. Spread Risk ──────────────────────
        spread_bps = market_features.get('spread_bps', 0) if market_features else 0
        spread_risk = self._score_inverse(spread_bps, low=1.0, high=self.MAX_SPREAD_BPS)
        factors.append({
            "name": "spread_bps",
            "value": spread_bps,
            "contrib": spread_risk,
            "explanation": f"Spread is {spread_bps:.1f} bps ({'tight' if spread_bps < 5 else 'wide' if spread_bps > 10 else 'moderate'})"
        })
        risk_scores.append(spread_risk)

        if spread_bps > self.MAX_SPREAD_BPS:
            gates.append(f"spread_too_wide ({spread_bps:.1f} > {self.MAX_SPREAD_BPS} bps)")

        # ── 2. Depth Risk ───────────────────────
        depth_usd = market_features.get('depth_usd_10bps', 0) if market_features else 0
        depth_risk = min(1.0, depth_usd / (self.MIN_DEPTH_USD * 20))  # scale to ~10k for full score
        factors.append({
            "name": "depth_usd_10bps",
            "value": depth_usd,
            "contrib": depth_risk,
            "explanation": f"Depth at 10bps: ${depth_usd:,.0f} ({'sufficient' if depth_usd > 5000 else 'thin'})"
        })
        risk_scores.append(depth_risk)

        if depth_usd < self.MIN_DEPTH_USD:
            gates.append(f"depth_too_thin (${depth_usd:.0f} < ${self.MIN_DEPTH_USD:.0f})")

        # ── 3. Slippage Risk ────────────────────
        slippage = market_features.get('slippage_bps_est', 0) if market_features else 0
        slippage_risk = self._score_inverse(slippage, low=2.0, high=self.MAX_SLIPPAGE_BPS)
        factors.append({
            "name": "slippage_bps_est",
            "value": slippage,
            "contrib": slippage_risk,
            "explanation": f"Est. slippage: {slippage:.1f} bps"
        })
        risk_scores.append(slippage_risk)

        if slippage > self.MAX_SLIPPAGE_BPS:
            gates.append(f"slippage_too_high ({slippage:.1f} > {self.MAX_SLIPPAGE_BPS} bps)")

        # ── 4. Position Concentration ───────────
        total_value = portfolio_state.get('total_value', 10000)
        positions = portfolio_state.get('positions', [])
        symbol_exposure = sum(
            float(p.get('qty', 0)) * float(p.get('average_entry_price', 0))
            for p in positions if p.get('symbol') == symbol
        )
        concentration = symbol_exposure / total_value if total_value > 0 else 0
        concentration_risk = self._score_inverse(concentration, low=0.0, high=self.MAX_POSITION_WEIGHT)
        factors.append({
            "name": "position_concentration",
            "value": concentration,
            "contrib": concentration_risk,
            "explanation": f"Position weight: {concentration*100:.1f}% of portfolio"
        })
        risk_scores.append(concentration_risk)

        if concentration > self.MAX_POSITION_WEIGHT:
            gates.append(f"position_too_concentrated ({concentration*100:.1f}% > {self.MAX_POSITION_WEIGHT*100:.0f}%)")

        # ── 5. Portfolio Volatility ─────────────
        vol_risk = self._score_inverse(realized_vol, low=0.0, high=self.MAX_PORTFOLIO_VOL_ANNUALIZED)
        factors.append({
            "name": "portfolio_vol",
            "value": realized_vol,
            "contrib": vol_risk,
            "explanation": f"Realized vol: {realized_vol*100:.1f}% annualized"
        })
        risk_scores.append(vol_risk)

        # ── 6. BTC Correlation ──────────────────
        btc_corr = await self._compute_btc_correlation(symbol, exchange_code)
        corr_risk = self._score_inverse(abs(btc_corr), low=0.0, high=self.MAX_BTC_CORR)
        factors.append({
            "name": "btc_corr",
            "value": btc_corr,
            "contrib": corr_risk,
            "explanation": f"Correlation with BTC: {btc_corr:.2f}"
        })
        risk_scores.append(corr_risk)

        if abs(btc_corr) > self.MAX_BTC_CORR:
            gates.append(f"btc_corr_too_high ({btc_corr:.2f})")

        # ── 7. Drawdown State ───────────────────
        initial_capital = portfolio_state.get('initial_capital', 10000)
        drawdown_pct = ((total_value - initial_capital) / initial_capital * 100) if initial_capital > 0 else 0
        drawdown_pct = min(0, drawdown_pct)  # only negative values
        drawdown_risk = self._score_inverse(abs(drawdown_pct), low=0.0, high=abs(self.MAX_DRAWDOWN_PCT))
        factors.append({
            "name": "drawdown_state",
            "value": drawdown_pct,
            "contrib": drawdown_risk,
            "explanation": f"Current drawdown: {drawdown_pct:.1f}%"
        })
        risk_scores.append(drawdown_risk)

        if drawdown_pct < self.MAX_DRAWDOWN_PCT:
            gates.append(f"drawdown_too_deep ({drawdown_pct:.1f}% < {self.MAX_DRAWDOWN_PCT}%)")

        # ── Composite S_risk ────────────────────
        if risk_scores:
            # Weighted average with heavier penalty for worst scores
            s_risk = sum(risk_scores) / len(risk_scores)
            # Additional penalty: if any gate is triggered, cap at 0.3
            if gates:
                s_risk = min(s_risk, 0.3)
        else:
            s_risk = 0.5

        s_risk = round(max(0.0, min(1.0, s_risk)), 4)

        return {
            "score": s_risk,
            "gates": gates,
            "factors": factors,
            "tradeable": len(gates) == 0,
        }

    async def _compute_btc_correlation(self, symbol: str, exchange_code: str,
                                        window_hours: int = 4) -> float:
        """
        Compute correlation between symbol returns and BTC returns over last N hours.
        Uses 1-minute returns from ohlcv_1s.
        """
        if 'BTC' in symbol:
            return 1.0

        async with self.db_pool.acquire() as conn:
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=window_hours)

            # Get minute-level closes for both
            symbol_rows = await conn.fetch("""
                SELECT time_bucket('1 minute', bucket_start) AS bucket, last(close, bucket_start) AS close
                FROM ohlcv_1s
                WHERE symbol = $1 AND bucket_start >= $2
                GROUP BY bucket
                ORDER BY bucket ASC
            """, symbol, start)

            btc_rows = await conn.fetch("""
                SELECT time_bucket('1 minute', bucket_start) AS bucket, last(close, bucket_start) AS close
                FROM ohlcv_1s
                WHERE symbol = 'BTC/USDT' AND bucket_start >= $1
                GROUP BY bucket
                ORDER BY bucket ASC
            """, start)

            if len(symbol_rows) < 10 or len(btc_rows) < 10:
                return 0.5  # insufficient data, assume moderate correlation

            # Align timestamps
            btc_map = {r['bucket']: float(r['close']) for r in btc_rows}
            aligned_sym = []
            aligned_btc = []
            for r in symbol_rows:
                if r['bucket'] in btc_map:
                    aligned_sym.append(float(r['close']))
                    aligned_btc.append(btc_map[r['bucket']])

            if len(aligned_sym) < 10:
                return 0.5

            # Compute returns
            sym_returns = [(aligned_sym[i] - aligned_sym[i-1]) / aligned_sym[i-1]
                          for i in range(1, len(aligned_sym)) if aligned_sym[i-1] > 0]
            btc_returns = [(aligned_btc[i] - aligned_btc[i-1]) / aligned_btc[i-1]
                          for i in range(1, len(aligned_btc)) if aligned_btc[i-1] > 0]

            n = min(len(sym_returns), len(btc_returns))
            if n < 5:
                return 0.5

            sym_returns = sym_returns[:n]
            btc_returns = btc_returns[:n]

            # Pearson correlation
            mean_s = sum(sym_returns) / n
            mean_b = sum(btc_returns) / n
            cov = sum((sym_returns[i] - mean_s) * (btc_returns[i] - mean_b) for i in range(n)) / n
            std_s = (sum((r - mean_s) ** 2 for r in sym_returns) / n) ** 0.5
            std_b = (sum((r - mean_b) ** 2 for r in btc_returns) / n) ** 0.5

            if std_s == 0 or std_b == 0:
                return 0.0

            corr = cov / (std_s * std_b)
            return round(max(-1.0, min(1.0, corr)), 4)

    @staticmethod
    def _score_inverse(value: float, low: float, high: float) -> float:
        """
        Maps a value to [0, 1] inversely: low value → 1.0 (safe), high value → 0.0 (risky).
        """
        if high <= low:
            return 0.5
        clamped = max(low, min(high, value))
        return 1.0 - ((clamped - low) / (high - low))
