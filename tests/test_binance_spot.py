"""
Offline tests for the Binance Spot live hub (no network).

Covers the parts that decide whether the cockpit matches Binance UI:
  * parsing every stream type (trade/aggTrade/ticker/bookTicker/kline/depth)
  * spread / mid / spread-bps math
  * PRICE_SOURCE selection
  * feed status LIVE / STALE / NODATA (freshness, never fabricated)
  * order-book snapshot + diff apply, and gap detection in update IDs
"""

import asyncio

import pytest

from market.binance_spot import (
    parse_trade, parse_agg_trade, parse_ticker, parse_book_ticker, parse_kline,
    parse_depth_update, parse_rest_klines, spread_bps, book_mid,
    OrderBook, DepthUpdate, SymbolState, BinanceSpotHub, PRICE_SOURCES,
    normalize_range, range_to_interval, klines_limit_for_range, VALID_INTERVALS,
)


def _kline_msg(native, t_ms, close, interval="1m"):
    n = native.lower()
    return ('{"stream":"' + n + '@kline_' + interval + '","data":{"e":"kline","E":1,"s":"' + native + '",'
            '"k":{"t":' + str(t_ms) + ',"T":' + str(t_ms + 59999) + ',"i":"' + interval + '","o":"1","c":"'
            + str(close) + '","h":"9","l":"0","v":"5","q":"0","n":0,"x":false}}}')


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parse_trade():
    ev = parse_trade({"e": "trade", "E": 1700000000100, "s": "BTCUSDT", "t": 42,
                      "p": "63185.94", "q": "0.01", "T": 1700000000000, "m": True})
    assert ev.native_symbol == "BTCUSDT"
    assert ev.price == 63185.94
    assert ev.qty == 0.01
    assert ev.ts_event_ms == 1700000000000  # prefers trade time T over event time E
    assert ev.is_buyer_maker is True
    assert ev.trade_id == 42
    assert ev.channel == "trade"


def test_parse_agg_trade():
    ev = parse_agg_trade({"e": "aggTrade", "E": 1700000000100, "s": "ETHUSDT",
                          "a": 99, "p": "1682.82", "q": "1.5", "T": 1700000000050, "m": False})
    assert ev.channel == "aggTrade"
    assert ev.price == 1682.82
    assert ev.trade_id == 99
    assert ev.ts_event_ms == 1700000000050


def test_parse_ticker():
    ev = parse_ticker({
        "e": "24hrTicker", "E": 1700000000000, "s": "BTCUSDT", "p": "1681.93",
        "P": "2.730", "w": "63000.0", "c": "63185.94", "o": "61504.01",
        "h": "64234.68", "l": "61500.00", "v": "27589.58", "q": "1747670449.94",
        "n": 1234567, "b": "63185.93", "a": "63185.95",
    })
    assert ev.last == 63185.94
    assert ev.price_change == 1681.93
    assert ev.price_change_pct == 2.730
    assert ev.high == 64234.68
    assert ev.volume_base == 27589.58
    assert ev.volume_quote == 1747670449.94
    assert ev.num_trades == 1234567
    assert ev.best_bid == 63185.93 and ev.best_ask == 63185.95


def test_parse_book_ticker():
    ev = parse_book_ticker({"u": 400900217, "s": "BTCUSDT", "b": "63185.93",
                            "B": "0.041", "a": "63185.95", "A": "0.104"})
    assert ev.bid_px == 63185.93 and ev.ask_px == 63185.95
    assert ev.bid_qty == 0.041 and ev.ask_qty == 0.104
    assert ev.update_id == 400900217
    assert ev.ts_event_ms is None  # raw bookTicker has no event time


def test_parse_kline():
    ev = parse_kline({
        "e": "kline", "E": 1700000000123, "s": "BTCUSDT",
        "k": {"t": 1700000000000, "T": 1700000059999, "s": "BTCUSDT", "i": "1m",
              "o": "63100.0", "c": "63185.94", "h": "63200.0", "l": "63050.0",
              "v": "12.5", "q": "789012.3", "n": 321, "x": False},
    })
    assert ev.interval == "1m"
    assert ev.open == 63100.0 and ev.close == 63185.94
    assert ev.is_closed is False
    assert ev.start_ms == 1700000000000


def test_parse_depth_update():
    du = parse_depth_update({"e": "depthUpdate", "E": 123, "s": "BTCUSDT", "U": 10, "u": 12,
                             "b": [["63185.0", "1.0"], ["63184.0", "0"]],
                             "a": [["63186.0", "2.0"]]})
    assert du.first_update_id == 10 and du.final_update_id == 12
    assert du.bids == [(63185.0, 1.0), (63184.0, 0.0)]
    assert du.asks == [(63186.0, 2.0)]


def test_parse_rest_klines():
    rows = [[1700000000000, "63100.0", "63200.0", "63050.0", "63185.94", "12.5",
             1700000059999, "789012.3", 321, "6.0", "378000.0", "0"]]
    candles = parse_rest_klines(rows)
    assert candles == [{"time": 1700000000, "open": 63100.0, "high": 63200.0,
                        "low": 63050.0, "close": 63185.94, "value": 12.5}]


# ── Microstructure math ──────────────────────────────────────────────────────

def test_spread_and_mid():
    assert book_mid(100.0, 102.0) == 101.0
    # spread_bps = (ask-bid)/mid*1e4 = 2/101*1e4 ≈ 198.02
    assert spread_bps(100.0, 102.0) == pytest.approx(198.0198, rel=1e-4)
    assert spread_bps(0, 102.0) is None
    assert book_mid(0, 102.0) is None


# ── PRICE_SOURCE selection ───────────────────────────────────────────────────

def _state_with_all():
    st = SymbolState(symbol="BTC/USDT", native_symbol="BTCUSDT")
    st.on_trade(parse_trade({"s": "BTCUSDT", "p": "100.0", "q": "1", "T": 1, "m": False, "t": 1}))
    st.on_agg_trade(parse_agg_trade({"s": "BTCUSDT", "p": "101.0", "q": "1", "T": 1, "m": False, "a": 1}))
    st.on_ticker(parse_ticker({"s": "BTCUSDT", "E": 1, "p": "0", "P": "0", "w": "0", "c": "102.0",
                               "o": "0", "h": "0", "l": "0", "v": "0", "q": "0", "n": 0,
                               "b": "103.0", "a": "105.0"}))
    st.on_book_ticker(parse_book_ticker({"s": "BTCUSDT", "u": 1, "b": "103.0", "B": "1",
                                         "a": "105.0", "A": "1"}))
    st.on_kline(parse_kline({"s": "BTCUSDT", "E": 1, "k": {"t": 0, "T": 1, "i": "1m", "o": "0",
                             "c": "106.0", "h": "0", "l": "0", "v": "0", "q": "0", "n": 0, "x": False}}))
    return st


@pytest.mark.parametrize("source,expected", [
    ("trade", 100.0),
    ("aggTrade", 101.0),
    ("ticker_last", 102.0),
    ("book_mid", 104.0),    # (103+105)/2
    ("kline_close", 106.0),
])
def test_price_source_selection(source, expected):
    st = _state_with_all()
    st.price_source = source
    assert st.displayed_price() == expected


def test_all_price_sources_valid():
    assert set(PRICE_SOURCES) == {"trade", "aggTrade", "ticker_last", "book_mid", "kline_close"}


# ── Feed status: live / stale / nodata ───────────────────────────────────────

def test_feed_status_nodata_before_any_event():
    st = SymbolState(symbol="BTC/USDT", native_symbol="BTCUSDT")
    assert st.feed_status(max_age_ms=3000) == "nodata"
    assert st.displayed_price() is None


def test_feed_status_live_then_stale():
    st = SymbolState(symbol="BTC/USDT", native_symbol="BTCUSDT")
    st.on_trade(parse_trade({"s": "BTCUSDT", "p": "100.0", "q": "1", "T": 1, "m": False, "t": 1}))
    recv = st.last_recv_ms
    # fresh: now == recv → age 0 → live
    assert st.feed_status(max_age_ms=3000, now_ms=recv) == "live"
    # 1s later, still within window → live
    assert st.feed_status(max_age_ms=3000, now_ms=recv + 1000) == "live"
    # 5s later, beyond window → stale
    assert st.feed_status(max_age_ms=3000, now_ms=recv + 5000) == "stale"
    assert st.staleness_ms(now_ms=recv + 5000) == 5000


def test_latency_ms():
    st = SymbolState(symbol="BTC/USDT", native_symbol="BTCUSDT")
    st.on_trade(parse_trade({"s": "BTCUSDT", "p": "100.0", "q": "1", "T": 1, "m": False, "t": 1}))
    # event time was 1ms epoch; receive is "now" → latency huge but non-negative
    assert st.latency_ms() is not None and st.latency_ms() >= 0


# ── Order book: snapshot + diff apply + gap detection ────────────────────────

def _du(U, u, bids=None, asks=None):
    return DepthUpdate("BTCUSDT", U, u, bids or [], asks or [], None)


def test_orderbook_buffered_before_snapshot():
    ob = OrderBook()
    assert ob.apply_update(_du(10, 12)) == "buffered"


def test_orderbook_snapshot_and_apply():
    ob = OrderBook()
    ob.apply_snapshot(100, [["100.0", "1.0"], ["99.0", "2.0"]], [["101.0", "1.0"], ["102.0", "3.0"]])
    assert ob.best_bid == 100.0 and ob.best_ask == 101.0
    assert ob.mid == 100.5
    # event fully covered by snapshot → skipped
    assert ob.apply_update(_du(90, 100)) == "skipped"
    # first valid event: U <= 101 <= u, contiguous from 100
    assert ob.apply_update(_du(101, 105, bids=[["100.0", "5.0"]])) == "applied"
    assert ob.bids[100.0] == 5.0
    assert ob.last_update_id == 105
    # remove a level with qty 0
    assert ob.apply_update(_du(106, 106, asks=[["101.0", "0"]])) == "applied"
    assert 101.0 not in ob.asks
    assert ob.best_ask == 102.0


def test_orderbook_gap_detection():
    ob = OrderBook()
    ob.apply_snapshot(100, [["100.0", "1.0"]], [["101.0", "1.0"]])
    assert ob.apply_update(_du(101, 105)) == "applied"
    # next event should start at 106; 110 means we missed 106..109 → gap
    assert ob.apply_update(_du(110, 115)) == "gap"


def test_orderbook_depth_and_imbalance():
    ob = OrderBook()
    # mid = 100.5; 10bps band = ±0.1005 → [100.3995, 100.6005]. The 100.00 bid and
    # 101.00 ask sit OUTSIDE the band and must be excluded from depth/imbalance.
    ob.apply_snapshot(
        1,
        [["100.49", "10.0"], ["100.00", "10.0"]],   # bids (100.00 outside band)
        [["100.51", "5.0"], ["101.00", "5.0"]],     # asks (101.00 outside band)
    )
    mid = ob.mid
    assert mid == pytest.approx(100.5, rel=1e-6)
    depth = ob.depth_usd(10.0)   # only 100.49 bid & 100.51 ask are within band
    assert depth == pytest.approx(100.49 * 10 + 100.51 * 5, rel=1e-6)
    imb = ob.imbalance(10.0)
    assert imb > 0  # more bid notional than ask within band


def test_orderbook_slippage_insufficient_liquidity():
    ob = OrderBook()
    ob.apply_snapshot(1, [["100.0", "1.0"]], [["101.0", "1.0"]])  # ~$101 on the ask
    assert ob.slippage_bps_est(1_000_000.0, "buy") is None  # cannot fill $1M


def test_orderbook_slippage_positive():
    ob = OrderBook()
    ob.apply_snapshot(1, [["100.0", "100.0"]], [["101.0", "100.0"]])
    s = ob.slippage_bps_est(100.0, "buy")  # tiny order, fills at 101 vs mid 100.5
    # avg 101 vs mid 100.5 → ~49.75 bps
    assert s == pytest.approx((101 - 100.5) / 100.5 * 10000, rel=1e-6)


# ── Hub wiring (no network) ──────────────────────────────────────────────────

def test_hub_stream_url_is_spot_combined():
    hub = BinanceSpotHub(["BTC/USDT", "ETH/USDT"], candle_interval="1m")
    url = hub._stream_url()
    assert url.startswith("wss://stream.binance.com:9443/stream?streams=")
    assert "btcusdt@trade" in url and "btcusdt@bookTicker" in url
    assert "btcusdt@kline_1m" in url and "btcusdt@depth@100ms" in url
    assert "ethusdt@ticker" in url
    assert "fstream" not in url  # never futures


def test_hub_dispatch_updates_state():
    hub = BinanceSpotHub(["BTC/USDT"], price_source="trade")
    # combined-stream envelope
    hub._handle_message('{"stream":"btcusdt@trade","data":{"e":"trade","E":1,"s":"BTCUSDT",'
                        '"t":1,"p":"63185.94","q":"0.01","T":1,"m":false}}')
    snap = hub.snapshot("BTC/USDT")
    assert snap["displayed_price"] == 63185.94
    assert snap["price_source"] == "trade"
    assert snap["source"] == "binance_spot"
    assert snap["raw"]["trade_price"] == 63185.94


def test_hub_unknown_symbol_returns_none():
    hub = BinanceSpotHub(["BTC/USDT"])
    assert hub.snapshot("DOGE/USDT") is None
    assert hub.has_symbol("BTC/USDT") is True


# ── Chart ranges (1D / 7D / 1M / 1Y) ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("1J", "1D"), ("1D", "1D"), ("7J", "7D"), ("7d", "7D"),
    ("1M", "1M"), ("1Mois", "1M"), ("1An", "1Y"), ("1y", "1Y"),
    (None, "1D"), ("garbage", "1D"),
])
def test_normalize_range(raw, expected):
    assert normalize_range(raw) == expected


def test_range_to_interval_default_and_custom():
    assert range_to_interval("1J") == "1m"       # default map
    assert range_to_interval("7D") == "15m"
    assert range_to_interval("1An") == "1d"
    custom = {"1D": "5m", "7D": "1h", "1M": "4h", "1Y": "1w"}
    assert range_to_interval("1D", custom) == "5m"
    assert range_to_interval("1Y", custom) == "1w"


def test_klines_limit_for_range_capped():
    # 1 day at 1m = 1440 candles → capped at 1000.
    assert klines_limit_for_range("1D", "1m", cap=1000) == 1000
    # 1 year at 1d = 365 candles → under the cap, exact.
    assert klines_limit_for_range("1Y", "1d", cap=1000) == 365
    # 7 days at 15m = 672 candles.
    assert klines_limit_for_range("7D", "15m", cap=1000) == 672
    # unknown interval → cap
    assert klines_limit_for_range("1D", "weird", cap=500) == 500


def test_valid_intervals_contains_common():
    for i in ("1s", "1m", "5m", "15m", "1h", "4h", "1d"):
        assert i in VALID_INTERVALS


# ── Dynamic active-symbol selection (Tier 3, bounded) ────────────────────────

def test_set_active_symbol_adds_and_evicts():
    hub = BinanceSpotHub(["BTC/USDT"], active_symbol_limit=2)
    # selecting an already-tracked (core) symbol → no reconnect
    res = asyncio.run(hub.set_active_symbol("BTC/USDT"))
    assert res["reconnect"] is False
    # add ETH (room for it)
    res = asyncio.run(hub.set_active_symbol("ETH/USDT"))
    assert res["reconnect"] is True
    assert hub.has_symbol("ETH/USDT")
    assert set(hub.symbols) == {"BTC/USDT", "ETH/USDT"}
    # add SOL → at the limit, evict the oldest dynamic (ETH); core BTC stays
    asyncio.run(hub.set_active_symbol("SOL/USDT"))
    assert hub.has_symbol("SOL/USDT")
    assert not hub.has_symbol("ETH/USDT")     # evicted
    assert hub.has_symbol("BTC/USDT")         # core never evicted
    assert hub.active_symbol == "SOL/USDT"
    assert len(hub.symbols) <= 2


def test_set_chart_interval_clears_cache_and_validates():
    hub = BinanceSpotHub(["BTC/USDT"], candle_interval="1m")
    hub._handle_message(_kline_msg("BTCUSDT", 60000, 2.0, "1m"))
    assert hub.klines("BTC/USDT")             # cache has a candle
    res = asyncio.run(hub.set_chart_interval("5m"))
    assert res["changed"] is True and hub.candle_interval == "5m"
    assert hub.klines("BTC/USDT") == []       # cache cleared on interval switch
    # same interval → no-op
    assert asyncio.run(hub.set_chart_interval("5m"))["changed"] is False
    # invalid interval rejected, interval unchanged
    bad = asyncio.run(hub.set_chart_interval("nope"))
    assert bad["ok"] is False and hub.candle_interval == "5m"


def test_set_range_maps_to_interval():
    hub = BinanceSpotHub(["BTC/USDT"], candle_interval="1m")
    res = asyncio.run(hub.set_range("7J"))
    assert res["range"] == "7D" and hub.candle_interval == "15m"


# ── Bounded kline cache (memory) ─────────────────────────────────────────────

def test_kline_cache_is_bounded():
    # max_candles is clamped up to kline_history (backfill must fit), so set both.
    hub = BinanceSpotHub(["BTC/USDT"], candle_interval="1m", kline_history=5, max_candles=5)
    for i in range(1, 11):                     # 10 distinct 1m buckets
        hub._handle_message(_kline_msg("BTCUSDT", i * 60000, float(i), "1m"))
    cache = hub.klines("BTC/USDT")
    assert len(cache) == 5                      # trimmed to max_candles
    assert cache[-1]["close"] == 10.0           # newest kept
    assert cache[0]["time"] == 6 * 60           # oldest five dropped


# ── Depth-only-for-selected + bounded depth buffer (memory) ──────────────────

def test_stream_url_depth_only_for_selected():
    hub = BinanceSpotHub(["BTC/USDT", "ETH/USDT"], candle_interval="1m", depth_only_selected=True)
    hub.active_symbol = "BTC/USDT"
    url = hub._stream_url()
    assert "btcusdt@depth@100ms" in url          # selected gets the book
    assert "ethusdt@depth@100ms" not in url       # others do not
    assert "ethusdt@trade" in url and "ethusdt@kline_1m" in url  # but keep light streams


def test_stream_url_depth_all_when_flag_off():
    hub = BinanceSpotHub(["BTC/USDT", "ETH/USDT"], candle_interval="1m", depth_only_selected=False)
    url = hub._stream_url()
    assert "btcusdt@depth@100ms" in url and "ethusdt@depth@100ms" in url


def test_depth_buffer_capped_while_unsynced():
    # depth_buffer_max has a sane floor of 100, so use that as the effective cap.
    hub = BinanceSpotHub(["BTC/USDT"], candle_interval="1m", depth_buffer_max=100)
    assert hub.states["BTC/USDT"].order_book.synced is False
    for i in range(300):
        hub._handle_message('{"stream":"btcusdt@depth@100ms","data":{"e":"depthUpdate","E":1,'
                            '"s":"BTCUSDT","U":' + str(i) + ',"u":' + str(i) + ',"b":[],"a":[]}}')
    assert len(hub._depth_buffer["BTC/USDT"]) <= 100   # bounded, never unbounded


# ── Interval switch resets live kline state (honest chart_status) ─────────────

def test_interval_switch_resets_live_kline_state():
    hub = BinanceSpotHub(["BTC/USDT"], candle_interval="1m")
    hub._handle_message(_kline_msg("BTCUSDT", 60000, 2.0, "1m"))
    st = hub.states["BTC/USDT"]
    assert st.kline is not None and st.kline_event_count == 1
    asyncio.run(hub.set_chart_interval("5m"))
    assert st.kline is None and st.kline_event_count == 0 and st.last_kline_recv_ms is None
    assert st.chart_status(max_age_ms=6000) == "nodata"   # honest until a 5m kline arrives


# ── Combined symbol+range applies once ───────────────────────────────────────

def test_set_active_and_range_applies_both():
    hub = BinanceSpotHub(["BTC/USDT"], candle_interval="1m", active_symbol_limit=5)
    res = asyncio.run(hub.set_active_and_range("ETH/USDT", "7J"))
    assert hub.has_symbol("ETH/USDT") and hub.active_symbol == "ETH/USDT"
    assert hub.candle_interval == "15m"
    assert res["reconnect"] is True and res["range"] == "7D"
