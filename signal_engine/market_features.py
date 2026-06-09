"""
Market Features Calculator
--------------------------
Computes real-time microstructure features from bbo_tick and trade_tick data.
Writes results into market_feature_1s hypertable.
"""

import asyncio
import asyncpg
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class MarketFeaturesCalculator:
    """Computes market microstructure features for a given symbol."""

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        # Cache for 24h volume averages (refreshed periodically)
        self._vol_avg_cache: Dict[str, float] = {}
        self._vol_avg_cache_time: Optional[datetime] = None

    async def compute_features(self, symbol: str, exchange_code: str) -> Optional[Dict[str, Any]]:
        """
        Compute all market features for a symbol from the latest available data.
        Returns a dict of features or None if insufficient data.
        """
        async with self.db_pool.acquire() as conn:
            # Latest BBO
            bbo = await conn.fetchrow("""
                SELECT ts_event, bid_px, bid_qty, ask_px, ask_qty
                FROM bbo_tick
                WHERE symbol = $1 AND exchange_code = $2
                ORDER BY ts_event DESC
                LIMIT 1
            """, symbol, exchange_code)

            if not bbo:
                return None

            # Age of the latest BBO — used by the staleness gate and audit.
            now = datetime.now(timezone.utc)
            data_age_ms = max(0.0, (now - bbo['ts_event']).total_seconds() * 1000.0)

            bid_px = float(bbo['bid_px'])
            ask_px = float(bbo['ask_px'])
            bid_qty = float(bbo['bid_qty'])
            ask_qty = float(bbo['ask_qty'])

            if bid_px <= 0 or ask_px <= 0:
                return None

            mid_px = (bid_px + ask_px) / 2.0

            # --- Spread ---
            spread_bps = ((ask_px - bid_px) / mid_px) * 10000

            # --- Depth estimation (10bps from mid) ---
            # Using top-of-book as proxy with heuristic multiplier
            depth_usd_10bps = (bid_px * bid_qty + ask_px * ask_qty) * 5.0

            # --- Book imbalance ---
            total_qty = bid_qty + ask_qty
            book_imbalance = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0

            # --- Trade pressure (buy vs sell volume in last 10s) ---
            trade_pressure = await self._compute_trade_pressure(conn, symbol, exchange_code)

            # --- Relative volume (current 1m volume vs 24h avg 1m volume) ---
            relative_volume = await self._compute_relative_volume(conn, symbol, exchange_code)

            # --- Slippage estimation ---
            # slippage_est = max(spread_bps/2, spread_bps + impact_coeff * typical_order / depth)
            typical_order_usd = 1000.0  # 10% of 10k portfolio
            impact_coeff = 0.5
            slippage_bps_est = max(
                spread_bps / 2.0,
                spread_bps + impact_coeff * (typical_order_usd / max(depth_usd_10bps, 1.0))
            )

            return {
                "ts": now,
                "symbol": symbol,
                "exchange_code": exchange_code,
                "data_age_ms": round(data_age_ms, 1),
                "spread_bps": round(spread_bps, 4),
                "depth_usd_10bps": round(depth_usd_10bps, 2),
                "book_imbalance": round(max(-1.0, min(1.0, book_imbalance)), 6),
                "trade_pressure": round(max(-1.0, min(1.0, trade_pressure)), 6),
                "relative_volume": round(relative_volume, 4),
                "slippage_bps_est": round(slippage_bps_est, 4),
                "bid_px": bid_px,
                "ask_px": ask_px,
                "mid_px": mid_px,
            }

    async def _compute_trade_pressure(self, conn: asyncpg.Connection,
                                       symbol: str, exchange_code: str,
                                       window_seconds: int = 10) -> float:
        """
        Compute buy vs sell trade pressure over the last N seconds.
        Returns value in [-1, +1]: +1 = all buys, -1 = all sells.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(seconds=window_seconds)

        row = await conn.fetchrow("""
            SELECT
                COALESCE(SUM(CASE WHEN side = 'buy' THEN qty * price ELSE 0 END), 0) AS buy_vol,
                COALESCE(SUM(CASE WHEN side = 'sell' THEN qty * price ELSE 0 END), 0) AS sell_vol
            FROM trade_tick
            WHERE symbol = $1 AND exchange_code = $2
              AND ts_event >= $3 AND ts_event < $4
        """, symbol, exchange_code, start, now)

        if not row:
            return 0.0

        buy_vol = float(row['buy_vol'])
        sell_vol = float(row['sell_vol'])
        total = buy_vol + sell_vol
        if total == 0:
            return 0.0

        return (buy_vol - sell_vol) / total

    async def _compute_relative_volume(self, conn: asyncpg.Connection,
                                        symbol: str, exchange_code: str) -> float:
        """
        Compare current minute's volume to the 24h average per-minute volume.
        Returns ratio: 1.0 = average, 2.0 = double average.
        """
        now = datetime.now(timezone.utc)
        one_min_ago = now - timedelta(minutes=1)
        twenty_four_h_ago = now - timedelta(hours=24)

        # Current minute volume
        current = await conn.fetchval("""
            SELECT COALESCE(SUM(volume_base), 0)
            FROM ohlcv_1s
            WHERE symbol = $1 AND exchange_code = $2
              AND bucket_start >= $3
        """, symbol, exchange_code, one_min_ago)

        # 24h average per-minute volume
        avg_row = await conn.fetchval("""
            SELECT COALESCE(AVG(min_vol), 0) FROM (
                SELECT SUM(volume_base) AS min_vol
                FROM ohlcv_1s
                WHERE symbol = $1 AND exchange_code = $2
                  AND bucket_start >= $3 AND bucket_start < $4
                GROUP BY time_bucket('1 minute', bucket_start)
            ) sub
        """, symbol, exchange_code, twenty_four_h_ago, one_min_ago)

        current_vol = float(current) if current else 0.0
        avg_vol = float(avg_row) if avg_row else 0.0

        if avg_vol <= 0:
            return 1.0

        return current_vol / avg_vol

    async def compute_returns(self, conn: asyncpg.Connection,
                               symbol: str, exchange_code: str) -> Dict[str, float]:
        """
        Compute multi-timeframe returns: 15m, 1h, 4h, 24h.
        Returns dict of return percentages.
        """
        now = datetime.now(timezone.utc)
        windows = {
            "ret_15m": timedelta(minutes=15),
            "ret_1h": timedelta(hours=1),
            "ret_4h": timedelta(hours=4),
            "ret_24h": timedelta(hours=24),
        }

        # Current price
        current_row = await conn.fetchrow("""
            SELECT close FROM ohlcv_1s
            WHERE symbol = $1 AND exchange_code = $2
            ORDER BY bucket_start DESC
            LIMIT 1
        """, symbol, exchange_code)

        if not current_row:
            return {k: 0.0 for k in windows}

        current_price = float(current_row['close'])
        if current_price <= 0:
            return {k: 0.0 for k in windows}

        results = {}
        for name, delta in windows.items():
            target_time = now - delta
            past_row = await conn.fetchrow("""
                SELECT close FROM ohlcv_1s
                WHERE symbol = $1 AND exchange_code = $2
                  AND bucket_start <= $3
                ORDER BY bucket_start DESC
                LIMIT 1
            """, symbol, exchange_code, target_time)

            if past_row and float(past_row['close']) > 0:
                past_price = float(past_row['close'])
                results[name] = round((current_price - past_price) / past_price, 6)
            else:
                results[name] = 0.0

        return results

    async def compute_realized_volatility(self, conn: asyncpg.Connection,
                                           symbol: str, exchange_code: str,
                                           window_minutes: int = 60) -> float:
        """
        Compute realized volatility from 1-minute returns over the last N minutes.
        Returns annualized volatility estimate.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=window_minutes)

        records = await conn.fetch("""
            SELECT bucket_start, close FROM ohlcv_1s
            WHERE symbol = $1 AND exchange_code = $2
              AND bucket_start >= $3
            ORDER BY bucket_start ASC
        """, symbol, exchange_code, start)

        if len(records) < 10:
            return 0.0

        prices = [float(r['close']) for r in records if float(r['close']) > 0]
        if len(prices) < 10:
            return 0.0

        # Log returns
        log_returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                import math
                log_returns.append(math.log(prices[i] / prices[i - 1]))

        if not log_returns:
            return 0.0

        # Standard deviation of log returns
        mean_ret = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_ret) ** 2 for r in log_returns) / len(log_returns)
        std_dev = variance ** 0.5

        # Annualize (approx 525600 minutes per year)
        annualized = std_dev * (525600 ** 0.5)
        return round(annualized, 6)

    # Idempotent upsert reused by the single- and batch-write paths.
    _UPSERT_SQL = """
        INSERT INTO market_feature_1s (
            ts, symbol, exchange_code,
            spread_bps, depth_usd_10bps, book_imbalance,
            trade_pressure, relative_volume, slippage_bps_est,
            bid_px, ask_px, mid_px
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (ts, symbol, exchange_code)
        DO UPDATE SET
            spread_bps = EXCLUDED.spread_bps,
            depth_usd_10bps = EXCLUDED.depth_usd_10bps,
            book_imbalance = EXCLUDED.book_imbalance,
            trade_pressure = EXCLUDED.trade_pressure,
            relative_volume = EXCLUDED.relative_volume,
            slippage_bps_est = EXCLUDED.slippage_bps_est,
            bid_px = EXCLUDED.bid_px,
            ask_px = EXCLUDED.ask_px,
            mid_px = EXCLUDED.mid_px
    """

    @staticmethod
    def _feature_row(f: Dict[str, Any]) -> tuple:
        """Positional args for one _UPSERT_SQL row."""
        return (
            f['ts'], f['symbol'], f['exchange_code'],
            f['spread_bps'], f['depth_usd_10bps'], f['book_imbalance'],
            f['trade_pressure'], f['relative_volume'], f['slippage_bps_est'],
            f['bid_px'], f['ask_px'], f['mid_px'],
        )

    async def write_features(self, features: Dict[str, Any]) -> None:
        """Write a single computed feature row to market_feature_1s (idempotent)."""
        async with self.db_pool.acquire() as conn:
            await conn.execute(self._UPSERT_SQL, *self._feature_row(features))

    async def write_features_many(self, features_list: List[Dict[str, Any]]) -> int:
        """Batch-write computed features in one DB round-trip via executemany.

        Idempotent (same ON CONFLICT upsert as write_features). Returns the number
        of rows written so callers can increment rows_written_total accurately.
        """
        if not features_list:
            return 0
        rows = [self._feature_row(f) for f in features_list]
        async with self.db_pool.acquire() as conn:
            await conn.executemany(self._UPSERT_SQL, rows)
        return len(rows)
