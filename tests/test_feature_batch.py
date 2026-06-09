"""
Batched feature-write tests (offline).

The feature worker now computes all (symbol, exchange) features concurrently and
persists them in ONE round-trip via MarketFeaturesCalculator.write_features_many
(executemany) instead of N sequential INSERTs. These tests lock that behaviour:
batched path uses a single executemany, the single-row path still works, and the
positional row layout matches the 12-column upsert.
"""

import asyncio

from signal_engine.market_features import MarketFeaturesCalculator


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.execute_calls = []
        self.executemany_calls = []

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))

    async def executemany(self, query, rows):
        self.executemany_calls.append((query, list(rows)))


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


def _feature(symbol, exchange, ts=1):
    return {
        "ts": ts, "symbol": symbol, "exchange_code": exchange,
        "spread_bps": 1.0, "depth_usd_10bps": 2.0, "book_imbalance": 0.3,
        "trade_pressure": 0.4, "relative_volume": 1.2, "slippage_bps_est": 5.0,
        "bid_px": 100.0, "ask_px": 100.1, "mid_px": 100.05,
    }


def test_write_features_many_single_round_trip():
    conn = _FakeConn()
    calc = MarketFeaturesCalculator(_FakePool(conn))
    feats = [_feature("BTC/USDT", "binance"), _feature("ETH/USDT", "binance"),
             _feature("BTC/USDT", "kraken")]

    written = asyncio.run(calc.write_features_many(feats))

    assert written == 3
    # Exactly one executemany, no per-row execute.
    assert len(conn.executemany_calls) == 1
    assert conn.execute_calls == []
    query, rows = conn.executemany_calls[0]
    assert "INSERT INTO market_feature_1s" in query
    assert "ON CONFLICT (ts, symbol, exchange_code)" in query
    # 3 rows, each a 12-tuple in column order (ts, symbol, exchange_code, ...).
    assert len(rows) == 3
    assert all(len(r) == 12 for r in rows)
    assert rows[0][1] == "BTC/USDT" and rows[0][2] == "binance"
    assert rows[2][2] == "kraken"


def test_write_features_many_empty_is_noop():
    conn = _FakeConn()
    calc = MarketFeaturesCalculator(_FakePool(conn))
    written = asyncio.run(calc.write_features_many([]))
    assert written == 0
    assert conn.executemany_calls == []
    assert conn.execute_calls == []


def test_write_features_single_still_works():
    conn = _FakeConn()
    calc = MarketFeaturesCalculator(_FakePool(conn))
    asyncio.run(calc.write_features(_feature("SOL/USDT", "binance")))
    # Single path uses execute with 12 positional args.
    assert len(conn.execute_calls) == 1
    assert conn.executemany_calls == []
    _query, args = conn.execute_calls[0]
    assert len(args) == 12
    assert args[1] == "SOL/USDT" and args[2] == "binance"
