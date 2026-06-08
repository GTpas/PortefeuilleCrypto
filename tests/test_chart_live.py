"""
Offline tests for the real-time CHART feed (no network).

These pin down the behaviour that fixes the "frozen chart" bug:
  * kline parsing + ms→seconds conversion for the chart
  * candle update classification (append / update-last / reject-older) — the rule
    that stops the frontend from calling lightweight-charts update() with a
    backwards time (which throws and silently freezes the whole chart)
  * chart freshness status: NO CANDLES (nodata) / CHART LIVE / CHART STALE,
    kept SEPARATE from the price feed so a frozen chart is visible
  * a kline for one symbol never leaks into another symbol's chart
  * per-symbol kline caches stay independent across a symbol change
  * the chart source is always the real Binance kline feed — never a mock
"""

import pytest

from market.binance_spot import (
    parse_kline, parse_rest_klines, classify_candle_update,
    SymbolState, BinanceSpotHub,
)


# ── ms → seconds conversion for the chart ────────────────────────────────────

def test_kline_candle_time_is_seconds():
    """The chart (lightweight-charts) expects seconds; Binance sends ms."""
    st = SymbolState(symbol="BTC/USDT", native_symbol="BTCUSDT")
    st.on_kline(parse_kline({
        "s": "BTCUSDT", "E": 1700000000123,
        "k": {"t": 1700000000000, "T": 1700000059999, "i": "1m", "o": "63100.0",
              "c": "63185.94", "h": "63200.0", "l": "63050.0", "v": "12.5",
              "q": "0", "n": 0, "x": False},
    }))
    snap = st.snapshot(max_age_ms=3000, now_ms=st.last_kline_recv_ms)
    assert snap["candle"]["time"] == 1700000000          # 1700000000000 ms // 1000
    assert snap["candle"]["close"] == 63185.94
    assert snap["candle"]["interval"] == "1m"


def test_rest_klines_time_is_seconds():
    rows = [[1700000060000, "1.0", "2.0", "0.5", "1.5", "10.0",
             1700000119999, "0", 0, "0", "0", "0"]]
    assert parse_rest_klines(rows)[0]["time"] == 1700000060


# ── candle update classification (the anti-freeze rule) ──────────────────────

def test_classify_candle_update_append_first_bar():
    assert classify_candle_update(None, 1700000000) == "append"


def test_classify_candle_update_update_same_bucket():
    # Same minute bucket arriving again → refresh the in-progress bar, not a new one.
    assert classify_candle_update(1700000000, 1700000000) == "update"


def test_classify_candle_update_append_new_bucket():
    assert classify_candle_update(1700000000, 1700000060) == "append"


def test_classify_candle_update_rejects_older():
    # A 1m kline at :00 arriving after a 1s-OHLCV backfill bar at :56 — the exact
    # case that used to throw in lightweight-charts and freeze the chart.
    assert classify_candle_update(1700000056, 1700000000) == "older"


# ── chart freshness status: NO CANDLES / CHART LIVE / CHART STALE ────────────

def test_chart_status_nodata_before_any_kline():
    st = SymbolState(symbol="BTC/USDT", native_symbol="BTCUSDT")
    # A trade alone (price feed) must NOT make the chart look live.
    from market.binance_spot import parse_trade
    st.on_trade(parse_trade({"s": "BTCUSDT", "p": "100.0", "q": "1", "T": 1, "m": False, "t": 1}))
    assert st.chart_status(max_age_ms=6000) == "nodata"
    assert st.candle_age_ms() is None
    assert st.kline_event_count == 0


def test_chart_status_live_then_stale():
    st = SymbolState(symbol="BTC/USDT", native_symbol="BTCUSDT")
    st.on_kline(parse_kline({
        "s": "BTCUSDT", "E": 1,
        "k": {"t": 0, "T": 1, "i": "1m", "o": "0", "c": "1.0", "h": "0", "l": "0",
              "v": "0", "q": "0", "n": 0, "x": False},
    }))
    recv = st.last_kline_recv_ms
    assert st.kline_event_count == 1
    # fresh kline → CHART LIVE
    assert st.chart_status(max_age_ms=6000, now_ms=recv) == "live"
    assert st.chart_status(max_age_ms=6000, now_ms=recv + 5000) == "live"
    # klines stopped for longer than the window → CHART STALE
    assert st.chart_status(max_age_ms=6000, now_ms=recv + 7000) == "stale"
    assert st.candle_age_ms(now_ms=recv + 7000) == 7000


def test_snapshot_exposes_chart_fields():
    st = SymbolState(symbol="BTC/USDT", native_symbol="BTCUSDT")
    st.on_kline(parse_kline({
        "s": "BTCUSDT", "E": 1,
        "k": {"t": 0, "T": 1, "i": "1m", "o": "0", "c": "1.0", "h": "0", "l": "0",
              "v": "0", "q": "0", "n": 0, "x": False},
    }))
    snap = st.snapshot(max_age_ms=3000, now_ms=st.last_kline_recv_ms, chart_max_age_ms=6000)
    assert snap["chart_source"] == "binance_kline"
    assert snap["chart_status"] == "live"
    assert snap["kline_event_count"] == 1
    assert snap["candle_age_ms"] == 0


# ── a kline for one symbol must never touch another symbol's chart ───────────

def test_kline_does_not_leak_across_symbols():
    hub = BinanceSpotHub(["BTC/USDT", "ETH/USDT"], candle_interval="1m")
    hub._handle_message(
        '{"stream":"btcusdt@kline_1m","data":{"e":"kline","E":1,"s":"BTCUSDT",'
        '"k":{"t":0,"T":1,"i":"1m","o":"1","c":"2","h":"3","l":"0","v":"5","q":"0","n":0,"x":false}}}'
    )
    btc = hub.snapshot("BTC/USDT")
    eth = hub.snapshot("ETH/USDT")
    assert btc["candle"] is not None and btc["candle"]["close"] == 2.0
    assert btc["chart_status"] == "live"
    # ETH received nothing → its chart stays NO CANDLES, never overwritten by BTC.
    assert eth["candle"] is None
    assert eth["chart_status"] == "nodata"


# ── per-symbol kline caches stay independent across a symbol change ──────────

def test_kline_caches_are_per_symbol():
    hub = BinanceSpotHub(["BTC/USDT", "ETH/USDT"], candle_interval="1m")
    hub._handle_message(
        '{"stream":"btcusdt@kline_1m","data":{"e":"kline","E":1,"s":"BTCUSDT",'
        '"k":{"t":60000,"T":119999,"i":"1m","o":"1","c":"2","h":"3","l":"0","v":"5","q":"0","n":0,"x":false}}}'
    )
    hub._handle_message(
        '{"stream":"ethusdt@kline_1m","data":{"e":"kline","E":1,"s":"ETHUSDT",'
        '"k":{"t":60000,"T":119999,"i":"1m","o":"10","c":"20","h":"30","l":"5","v":"1","q":"0","n":0,"x":false}}}'
    )
    btc_candles = hub.klines("BTC/USDT")
    eth_candles = hub.klines("ETH/USDT")
    assert btc_candles and btc_candles[-1]["close"] == 2.0 and btc_candles[-1]["time"] == 60
    assert eth_candles and eth_candles[-1]["close"] == 20.0
    # switching symbol = reading the other cache; they never cross-contaminate.
    assert hub.snapshot("BTC/USDT")["candle"]["close"] == 2.0
    assert hub.snapshot("ETH/USDT")["candle"]["close"] == 20.0


def test_kline_cache_updates_in_progress_bar_not_appends():
    """Two klines in the same bucket refresh ONE bar (no duplicate candles)."""
    hub = BinanceSpotHub(["BTC/USDT"], candle_interval="1m")
    for close in ("2", "2.5", "3"):
        hub._handle_message(
            '{"stream":"btcusdt@kline_1m","data":{"e":"kline","E":1,"s":"BTCUSDT",'
            '"k":{"t":60000,"T":119999,"i":"1m","o":"1","c":"' + close + '",'
            '"h":"3","l":"0","v":"5","q":"0","n":0,"x":false}}}'
        )
    candles = hub.klines("BTC/USDT")
    assert len(candles) == 1            # same bucket → updated in place
    assert candles[-1]["close"] == 3.0


# ── chart source is always the real Binance kline feed — never a mock ────────

def test_chart_source_is_never_mock_under_binance_spot():
    hub = BinanceSpotHub(["BTC/USDT"], candle_interval="1m")
    hub._handle_message(
        '{"stream":"btcusdt@kline_1m","data":{"e":"kline","E":1,"s":"BTCUSDT",'
        '"k":{"t":0,"T":1,"i":"1m","o":"1","c":"2","h":"3","l":"0","v":"5","q":"0","n":0,"x":false}}}'
    )
    snap = hub.snapshot("BTC/USDT")
    assert snap["chart_source"] == "binance_kline"
    assert snap["source"] == "binance_spot"
    assert "mock" not in str(snap.get("chart_source")).lower()
