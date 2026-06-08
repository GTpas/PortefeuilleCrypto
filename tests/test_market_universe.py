"""
Offline tests for the market universe (Tier 1, light) — no network.

Covers the parts that decide which 300 symbols show up and in what order:
  * stablecoin / leverage-token exclusion (incl. the JUP false-positive guard)
  * canonical symbol derivation
  * REST 24h + WS !ticker@arr parsing
  * trending score ordering (volume / move / trades / spread)
  * filtering (min volume, valid-spot set) + ranking (sort, cap, rank index)
"""

import pytest

from market.universe import (
    is_stablecoin, is_leverage_token, to_canonical,
    parse_rest_24h, parse_arr_ticker, trending_score,
    passes_filters, rank_universe, UniverseTicker, STABLE_BASES,
)


def mk(symbol="BTC/USDT", last=100.0, qv=1e9, n=1_000_000, chg=2.0,
       high=105.0, low=95.0, bid=99.95, ask=100.05, recv=1000):
    base, quote = symbol.split("/")
    return UniverseTicker(
        symbol=symbol, base=base, quote=quote, last=last, open=last,
        high=high, low=low, change_pct=chg, quote_volume=qv,
        base_volume=(qv / last if last else 0.0), num_trades=int(n),
        best_bid=bid, best_ask=ask, ts_ms=recv, recv_ms=recv,
    )


# ── Exclusions ───────────────────────────────────────────────────────────────

def test_is_stablecoin():
    assert is_stablecoin("USDC") and is_stablecoin("FDUSD") and is_stablecoin("eur")
    assert not is_stablecoin("BTC") and not is_stablecoin("PEPE")
    assert "USDT" in STABLE_BASES


@pytest.mark.parametrize("base,expected", [
    ("ETH3L", True), ("BTC5S", True), ("SUSHIUP", True), ("BTCDOWN", True),
    ("ETHUP", True), ("1000SHIBUP", True),
    ("JUP", False),   # genuine token ending in "UP" — prefix too short to be leverage
    ("BTC", False), ("ETH", False), ("PEPE", False), ("SUN", False),
])
def test_is_leverage_token(base, expected):
    assert is_leverage_token(base) is expected


def test_to_canonical():
    assert to_canonical("BTCUSDT", "USDT") == "BTC/USDT"
    assert to_canonical("1000PEPEUSDT", "USDT") == "1000PEPE/USDT"
    assert to_canonical("BTCBUSD", "USDT") is None    # wrong quote
    assert to_canonical("USDT", "USDT") is None        # not <base><quote>


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parse_rest_24h():
    t = parse_rest_24h({
        "symbol": "BTCUSDT", "lastPrice": "63185.94", "openPrice": "61504.0",
        "highPrice": "64234.68", "lowPrice": "61500.0", "priceChangePercent": "2.73",
        "quoteVolume": "1747670449.94", "volume": "27589.58", "count": "1234567",
        "bidPrice": "63185.93", "askPrice": "63185.95", "closeTime": "1700000000000",
    }, "USDT")
    assert t.symbol == "BTC/USDT" and t.base == "BTC"
    assert t.last == 63185.94 and t.change_pct == 2.73
    assert t.quote_volume == pytest.approx(1747670449.94)
    assert t.num_trades == 1234567
    assert t.best_bid == 63185.93 and t.best_ask == 63185.95


def test_parse_rest_24h_wrong_quote_returns_none():
    assert parse_rest_24h({"symbol": "BTCBUSD", "lastPrice": "1"}, "USDT") is None


def test_parse_arr_ticker():
    t = parse_arr_ticker({
        "e": "24hrTicker", "E": 1700000000000, "s": "ETHUSDT", "c": "1682.82",
        "o": "1640.0", "h": "1700.0", "l": "1620.0", "P": "2.6", "q": "500000000",
        "v": "300000", "n": 654321, "b": "1682.81", "a": "1682.83",
    }, "USDT")
    assert t.symbol == "ETH/USDT" and t.last == 1682.82
    assert t.quote_volume == 500000000.0 and t.num_trades == 654321


# ── Microstructure helpers ───────────────────────────────────────────────────

def test_ticker_spread_and_range():
    t = mk(bid=100.0, ask=100.1, last=100.0, high=110.0, low=90.0)
    assert t.spread_bps() == pytest.approx((0.1 / 100.05) * 10000, rel=1e-4)
    assert t.volatility_range() == pytest.approx(0.2, rel=1e-6)  # (110-90)/100


# ── Trending score ordering ──────────────────────────────────────────────────

def test_trending_score_volume_dominates():
    hi = mk(qv=5e9)
    lo = mk(qv=1e6)
    assert trending_score(hi, now_ms=1000) > trending_score(lo, now_ms=1000)


def test_trending_score_move_increases():
    calm = mk(chg=0.5)
    wild = mk(chg=12.0)
    assert trending_score(wild, now_ms=1000) > trending_score(calm, now_ms=1000)


def test_trending_score_stale_penalty():
    fresh = mk(recv=1000)
    # Same data but evaluated far in the future → stale penalty lowers the score.
    s_fresh = trending_score(fresh, now_ms=1000, stale_ms=15000)
    s_stale = trending_score(fresh, now_ms=1000 + 60_000, stale_ms=15000)
    assert s_stale < s_fresh


# ── Filtering + ranking ──────────────────────────────────────────────────────

def test_passes_filters_excludes_stable_leverage_lowvol():
    assert not passes_filters(mk("USDC/USDT"), exclude_stables=True, exclude_leverage=True, min_quote_volume=0)
    assert not passes_filters(mk("ETH3L/USDT"), exclude_stables=True, exclude_leverage=True, min_quote_volume=0)
    assert not passes_filters(mk("DOGE/USDT", qv=1000), exclude_stables=True, exclude_leverage=True, min_quote_volume=1e6)
    assert passes_filters(mk("DOGE/USDT", qv=1e8), exclude_stables=True, exclude_leverage=True, min_quote_volume=1e6)


def test_passes_filters_valid_spot_set():
    assert passes_filters(mk("BTC/USDT"), exclude_stables=True, exclude_leverage=True,
                          min_quote_volume=0, valid_spot={"BTC/USDT"})
    assert not passes_filters(mk("XYZ/USDT"), exclude_stables=True, exclude_leverage=True,
                              min_quote_volume=0, valid_spot={"BTC/USDT"})


def test_rank_universe_sorts_caps_and_indexes():
    tickers = [
        mk("AAA/USDT", qv=1e9),
        mk("BBB/USDT", qv=5e9),     # highest volume → should rank #1
        mk("CCC/USDT", qv=2e6),
        mk("USDC/USDT", qv=9e9),    # stable → excluded despite huge volume
        mk("ETH3L/USDT", qv=8e9),   # leverage → excluded
    ]
    rows = rank_universe(tickers, limit=2, exclude_stables=True, exclude_leverage=True,
                         min_quote_volume=0, now_ms=1000)
    assert len(rows) == 2                          # capped at limit
    assert rows[0]["symbol"] == "BBB/USDT"          # highest score first
    assert rows[0]["rank"] == 1 and rows[1]["rank"] == 2
    syms = {r["symbol"] for r in rows}
    assert "USDC/USDT" not in syms and "ETH3L/USDT" not in syms


def test_rank_universe_respects_min_volume():
    tickers = [mk("AAA/USDT", qv=1e9), mk("BBB/USDT", qv=10_000)]
    rows = rank_universe(tickers, limit=10, min_quote_volume=1_000_000, now_ms=1000)
    assert [r["symbol"] for r in rows] == ["AAA/USDT"]


def test_row_shape_has_required_fields():
    rows = rank_universe([mk("BTC/USDT")], limit=1, min_quote_volume=0, now_ms=1000)
    r = rows[0]
    for key in ("symbol", "price", "change_pct", "quote_volume", "num_trades",
                "spread_bps", "trending_score", "rank", "stale", "source"):
        assert key in r
    assert r["source"] == "binance_spot"
