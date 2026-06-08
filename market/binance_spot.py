"""
Binance Spot live hub
=====================

A self-contained, in-process real-time layer for Binance **Spot** market data.
It exists to fix a concrete cockpit bug: the displayed price used to come from
``Binance aggTrade → trade_tick → aggregator (every 2s) → ohlcv_1s → /ws/live``,
which lags Binance UI by several seconds *and* could silently mix exchanges
(the ohlcv query did not pin ``exchange_code``). This hub instead keeps the
latest Binance Spot values in memory and serves them to the cockpit directly,
so the displayed value matches Binance UI within network latency — and is
*explicitly* sourced (``price_source``) and freshness-tracked.

Design split (so it is testable offline, no network):

* **Pure parsers** — ``parse_trade/agg_trade/ticker/book_ticker/kline/depth``
  turn one Binance message payload into a small dataclass. Zero I/O.
* **OrderBook** — applies a REST snapshot + diff-depth updates following the
  documented Binance procedure, with update-id **gap detection** → resync.
* **SymbolState** — holds the latest of every stream for one symbol, computes
  the displayed price for any ``PRICE_SOURCE``, microstructure, and freshness.
* **BinanceSpotHub** — the only part that does I/O: one combined-stream WS for
  all configured symbols + REST snapshots (klines/depth/ticker). Everything it
  computes is read back through ``snapshot()`` by the API/WS handlers.

Streams used (Spot, combined):
  ``<sym>@trade`` ``<sym>@aggTrade`` ``<sym>@ticker`` ``<sym>@bookTicker``
  ``<sym>@kline_<interval>`` ``<sym>@depth@100ms``
REST init: ``/api/v3/klines`` (chart history), ``/api/v3/depth`` (book snapshot),
``/api/v3/ticker/24hr`` (initial 24h stats).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Valid PRICE_SOURCE values (kept here so config + tests share one source of truth).
PRICE_SOURCES = ("trade", "aggTrade", "ticker_last", "book_mid", "kline_close")

# Streams subscribed per symbol (Spot). Order is irrelevant.
STREAM_SUFFIXES = ("trade", "aggTrade", "ticker", "bookTicker", "depth@100ms")

# Binance kline intervals we accept for the chart.
VALID_INTERVALS = ("1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h",
                   "6h", "8h", "12h", "1d", "3d", "1w", "1M")

# ── Chart ranges (1D / 7D / 1M / 1Y) ─────────────────────────────────────────
# Canonical range keys + French UI aliases (1J / 7J / 1An). The interval used for
# each range is configurable; the mapping below is *only* the time-window each
# range spans, used to size the REST history request.
CHART_RANGES = ("1D", "7D", "1M", "1Y")
_RANGE_ALIASES = {
    "1J": "1D", "1D": "1D", "24H": "1D",
    "7J": "7D", "7D": "7D", "1W": "7D",
    "1MO": "1M", "1MOIS": "1M", "1M": "1M", "30D": "1M",
    "1AN": "1Y", "1A": "1Y", "1Y": "1Y", "12M": "1Y", "365D": "1Y",
}
RANGE_WINDOW_MS = {
    "1D": 24 * 60 * 60 * 1000,
    "7D": 7 * 24 * 60 * 60 * 1000,
    "1M": 30 * 24 * 60 * 60 * 1000,
    "1Y": 365 * 24 * 60 * 60 * 1000,
}
INTERVAL_MS = {
    "1s": 1000, "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
    "3d": 259_200_000, "1w": 604_800_000, "1M": 2_592_000_000,
}
# Default range→interval mapping (overridable from config / .env).
DEFAULT_RANGE_INTERVALS = {"1D": "1m", "7D": "15m", "1M": "1h", "1Y": "1d"}


def normalize_range(range_key: Optional[str], default: str = "1D") -> str:
    """Map a UI range (incl. French aliases 1J/7J/1An) to a canonical key."""
    if not range_key:
        return default
    return _RANGE_ALIASES.get(str(range_key).strip().upper(), default)


def range_to_interval(range_key: Optional[str],
                      intervals: Optional[dict] = None, default: str = "1D") -> str:
    """Canonical range → Binance kline interval (pure)."""
    intervals = intervals or DEFAULT_RANGE_INTERVALS
    rk = normalize_range(range_key, default)
    return intervals.get(rk, DEFAULT_RANGE_INTERVALS.get(rk, "1m"))


def klines_limit_for_range(range_key: Optional[str], interval: str, cap: int = 1000) -> int:
    """
    How many candles to request for a range at an interval. Capped at ``cap``
    (Binance REST max 1000). Returns ``cap`` when either is unknown.
    """
    rk = normalize_range(range_key)
    window = RANGE_WINDOW_MS.get(rk)
    ims = INTERVAL_MS.get(interval)
    if not window or not ims:
        return cap
    return max(1, min(cap, math.ceil(window / ims)))


def to_native(symbol: str) -> str:
    """``BTC/USDT`` → ``btcusdt`` (Binance native stream symbol)."""
    return symbol.replace("/", "").lower()


def to_upper_native(symbol: str) -> str:
    """``BTC/USDT`` → ``BTCUSDT`` (Binance REST symbol / message ``s`` field)."""
    return symbol.replace("/", "").upper()


# ── Parsed event dataclasses ─────────────────────────────────────────────────

@dataclass
class TradeEvent:
    native_symbol: str
    price: float
    qty: float
    ts_event_ms: int
    is_buyer_maker: bool
    trade_id: Optional[int]
    channel: str  # "trade" | "aggTrade"


@dataclass
class TickerEvent:
    native_symbol: str
    last: float
    price_change: float
    price_change_pct: float
    weighted_avg: float
    open: float
    high: float
    low: float
    volume_base: float
    volume_quote: float
    num_trades: int
    best_bid: float
    best_ask: float
    ts_event_ms: int


@dataclass
class BookTickerEvent:
    native_symbol: str
    bid_px: float
    bid_qty: float
    ask_px: float
    ask_qty: float
    update_id: Optional[int]
    ts_event_ms: Optional[int]


@dataclass
class KlineEvent:
    native_symbol: str
    interval: str
    start_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float
    volume_base: float
    volume_quote: float
    num_trades: int
    is_closed: bool
    ts_event_ms: int


@dataclass
class DepthUpdate:
    native_symbol: str
    first_update_id: int  # U
    final_update_id: int  # u
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    ts_event_ms: Optional[int]


# ── Pure parsers ─────────────────────────────────────────────────────────────
# Each takes the Binance payload dict (already unwrapped from any combined-stream
# envelope) and returns a typed event. They never raise on *missing optional*
# fields; malformed payloads raise (caller logs + drops the message).

def parse_trade(d: dict) -> TradeEvent:
    return TradeEvent(
        native_symbol=d["s"],
        price=float(d["p"]),
        qty=float(d["q"]),
        ts_event_ms=int(d.get("T") or d.get("E")),
        is_buyer_maker=bool(d.get("m", False)),
        trade_id=int(d["t"]) if d.get("t") is not None else None,
        channel="trade",
    )


def parse_agg_trade(d: dict) -> TradeEvent:
    return TradeEvent(
        native_symbol=d["s"],
        price=float(d["p"]),
        qty=float(d["q"]),
        ts_event_ms=int(d.get("T") or d.get("E")),
        is_buyer_maker=bool(d.get("m", False)),
        trade_id=int(d["a"]) if d.get("a") is not None else None,
        channel="aggTrade",
    )


def parse_ticker(d: dict) -> TickerEvent:
    return TickerEvent(
        native_symbol=d["s"],
        last=float(d["c"]),
        price_change=float(d["p"]),
        price_change_pct=float(d["P"]),
        weighted_avg=float(d["w"]),
        open=float(d["o"]),
        high=float(d["h"]),
        low=float(d["l"]),
        volume_base=float(d["v"]),
        volume_quote=float(d["q"]),
        num_trades=int(d["n"]),
        best_bid=float(d["b"]),
        best_ask=float(d["a"]),
        ts_event_ms=int(d.get("E") or d.get("C") or 0),
    )


def parse_book_ticker(d: dict) -> BookTickerEvent:
    return BookTickerEvent(
        native_symbol=d["s"],
        bid_px=float(d["b"]),
        bid_qty=float(d["B"]),
        ask_px=float(d["a"]),
        ask_qty=float(d["A"]),
        update_id=int(d["u"]) if d.get("u") is not None else None,
        ts_event_ms=int(d["E"]) if d.get("E") is not None else None,
    )


def parse_kline(d: dict) -> KlineEvent:
    k = d["k"]
    return KlineEvent(
        native_symbol=d["s"],
        interval=k["i"],
        start_ms=int(k["t"]),
        close_ms=int(k["T"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume_base=float(k["v"]),
        volume_quote=float(k["q"]),
        num_trades=int(k.get("n", 0)),
        is_closed=bool(k.get("x", False)),
        ts_event_ms=int(d.get("E") or k["T"]),
    )


def parse_depth_update(d: dict) -> DepthUpdate:
    return DepthUpdate(
        native_symbol=d["s"],
        first_update_id=int(d["U"]),
        final_update_id=int(d["u"]),
        bids=[(float(px), float(qty)) for px, qty in d.get("b", [])],
        asks=[(float(px), float(qty)) for px, qty in d.get("a", [])],
        ts_event_ms=int(d["E"]) if d.get("E") is not None else None,
    )


def parse_rest_klines(rows: list) -> list[dict]:
    """REST ``/api/v3/klines`` rows → lightweight-charts candles (time in seconds)."""
    out = []
    for r in rows:
        out.append({
            "time": int(r[0]) // 1000,
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "value": float(r[5]),
        })
    return out


def classify_candle_update(last_time: Optional[int], new_time: int) -> str:
    """
    Decide how a new candle relates to the current chart series. Mirrors the
    frontend's safe-update logic so the chart never calls lightweight-charts'
    ``update()`` with a backwards time (which throws and silently freezes the
    whole chart — the original "frozen chart" bug).
      "append" — first bar, or a strictly newer bucket (``new_time > last_time``)
      "update" — same bucket → refresh the in-progress bar (``new_time == last_time``)
      "older"  — ``new_time < last_time`` → must NOT be passed to ``update()`` as-is
    """
    if last_time is None or new_time > last_time:
        return "append"
    if new_time == last_time:
        return "update"
    return "older"


# ── Microstructure helpers (pure) ────────────────────────────────────────────

def spread_bps(bid: float, ask: float) -> Optional[float]:
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10000.0


def book_mid(bid: float, ask: float) -> Optional[float]:
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


# ── Order book ───────────────────────────────────────────────────────────────

class OrderBook:
    """
    Local Binance Spot order book maintained from a REST snapshot + diff-depth
    stream, following the documented sequencing rules with gap detection.

    apply_update() returns one of:
      "skipped"  — event fully covered by the snapshot (u <= lastUpdateId)
      "applied"  — event applied, lastUpdateId advanced
      "gap"      — a missed update was detected (U > lastUpdateId + 1) → resync
    """

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update_id: Optional[int] = None
        self.synced: bool = False

    def apply_snapshot(self, last_update_id: int, bids, asks) -> None:
        self.bids = {float(px): float(qty) for px, qty in bids if float(qty) > 0}
        self.asks = {float(px): float(qty) for px, qty in asks if float(qty) > 0}
        self.last_update_id = int(last_update_id)
        self.synced = True

    def _apply_levels(self, side: dict[float, float], levels) -> None:
        for px, qty in levels:
            px = float(px)
            qty = float(qty)
            if qty == 0:
                side.pop(px, None)
            else:
                side[px] = qty

    def apply_update(self, du: DepthUpdate) -> str:
        if not self.synced or self.last_update_id is None:
            return "buffered"
        # Old event already represented in the snapshot.
        if du.final_update_id <= self.last_update_id:
            return "skipped"
        # Missing updates between what we have and this event.
        if du.first_update_id > self.last_update_id + 1:
            return "gap"
        self._apply_levels(self.bids, du.bids)
        self._apply_levels(self.asks, du.asks)
        self.last_update_id = du.final_update_id
        return "applied"

    # — derived metrics —
    @property
    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        return book_mid(self.best_bid or 0.0, self.best_ask or 0.0)

    def depth_usd(self, bps: float = 10.0) -> Optional[float]:
        """Total quote-notional resting within ``bps`` of mid on both sides."""
        mid = self.mid
        if mid is None:
            return None
        band = mid * bps / 10000.0
        lo, hi = mid - band, mid + band
        bid_usd = sum(px * qty for px, qty in self.bids.items() if px >= lo)
        ask_usd = sum(px * qty for px, qty in self.asks.items() if px <= hi)
        return bid_usd + ask_usd

    def imbalance(self, bps: float = 10.0) -> Optional[float]:
        """(bidUSD - askUSD) / (bidUSD + askUSD) within ``bps`` of mid ∈ [-1, 1]."""
        mid = self.mid
        if mid is None:
            return None
        band = mid * bps / 10000.0
        lo, hi = mid - band, mid + band
        bid_usd = sum(px * qty for px, qty in self.bids.items() if px >= lo)
        ask_usd = sum(px * qty for px, qty in self.asks.items() if px <= hi)
        tot = bid_usd + ask_usd
        if tot <= 0:
            return None
        return (bid_usd - ask_usd) / tot

    def slippage_bps_est(self, notional_usd: float, side: str = "buy") -> Optional[float]:
        """
        Estimate execution slippage (bps vs mid) to fill ``notional_usd`` by
        walking the book. side='buy' lifts asks, 'sell' hits bids.
        Returns None if the book cannot fill the notional.
        """
        mid = self.mid
        if mid is None or notional_usd <= 0:
            return None
        levels = sorted(self.asks.items()) if side == "buy" else sorted(self.bids.items(), reverse=True)
        remaining = notional_usd  # quote-notional still to fill
        base_qty = 0.0            # base units acquired
        for px, qty in levels:
            take = min(px * qty, remaining)  # quote spent at this level
            base_qty += take / px
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0 or base_qty <= 0:
            return None  # not enough liquidity to fill the notional
        avg_px = notional_usd / base_qty
        return abs(avg_px - mid) / mid * 10000.0


# ── Per-symbol live state ────────────────────────────────────────────────────

@dataclass
class SymbolState:
    symbol: str               # canonical "BTC/USDT"
    native_symbol: str        # "BTCUSDT"
    price_source: str = "trade"

    # latest raw events
    trade: Optional[TradeEvent] = None
    agg_trade: Optional[TradeEvent] = None
    ticker: Optional[TickerEvent] = None
    book: Optional[BookTickerEvent] = None
    kline: Optional[KlineEvent] = None
    order_book: OrderBook = field(default_factory=OrderBook)

    # bookkeeping
    last_event_ms: Optional[int] = None      # max Binance event time seen
    last_recv_ms: Optional[int] = None       # local wall-clock of last event
    last_price_event_ms: Optional[int] = None  # event time of the chosen price source
    last_kline_recv_ms: Optional[int] = None  # local wall-clock of last *kline* event (drives CHART status)
    kline_event_count: int = 0                # number of kline events applied (chart liveness proof)

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    # — displayed price selection —
    def displayed_price(self) -> Optional[float]:
        return self.price_for(self.price_source)

    def price_for(self, source: str) -> Optional[float]:
        if source == "trade":
            return self.trade.price if self.trade else None
        if source == "aggTrade":
            return self.agg_trade.price if self.agg_trade else None
        if source == "ticker_last":
            return self.ticker.last if self.ticker else None
        if source == "book_mid":
            if self.book:
                return book_mid(self.book.bid_px, self.book.ask_px)
            return None
        if source == "kline_close":
            return self.kline.close if self.kline else None
        return None

    def _price_event_ms(self, source: str) -> Optional[int]:
        if source == "trade":
            return self.trade.ts_event_ms if self.trade else None
        if source == "aggTrade":
            return self.agg_trade.ts_event_ms if self.agg_trade else None
        if source == "ticker_last":
            return self.ticker.ts_event_ms if self.ticker else None
        if source == "book_mid":
            return self.book.ts_event_ms if self.book else None
        if source == "kline_close":
            return self.kline.ts_event_ms if self.kline else None
        return None

    # — freshness / status —
    def staleness_ms(self, now_ms: Optional[int] = None) -> Optional[float]:
        """Age of the *displayed price* against local receive time."""
        if self.last_recv_ms is None or self.displayed_price() is None:
            return None
        now_ms = now_ms if now_ms is not None else self._now_ms()
        return max(0.0, now_ms - self.last_recv_ms)

    def latency_ms(self) -> Optional[float]:
        """Binance event-time → local-receive latency for the last event."""
        if self.last_event_ms is None or self.last_recv_ms is None:
            return None
        return max(0.0, self.last_recv_ms - self.last_event_ms)

    def feed_status(self, max_age_ms: int, now_ms: Optional[int] = None) -> str:
        """live | stale | nodata — never fabricated."""
        if self.displayed_price() is None:
            return "nodata"
        age = self.staleness_ms(now_ms)
        if age is None:
            return "nodata"
        return "live" if age <= max_age_ms else "stale"

    # — chart (kline) freshness, kept SEPARATE from the price feed so a frozen
    #   chart is visible even while the price ticker keeps moving —
    def candle_age_ms(self, now_ms: Optional[int] = None) -> Optional[float]:
        """Age of the last *kline* event against local receive time."""
        if self.last_kline_recv_ms is None:
            return None
        now_ms = now_ms if now_ms is not None else self._now_ms()
        return max(0.0, now_ms - self.last_kline_recv_ms)

    def chart_status(self, max_age_ms: int, now_ms: Optional[int] = None) -> str:
        """nodata (no candle yet) | live (fresh kline) | stale (klines stopped). Never fabricated."""
        if self.kline is None:
            return "nodata"
        age = self.candle_age_ms(now_ms)
        if age is None:
            return "nodata"
        return "live" if age <= max_age_ms else "stale"

    # — microstructure (prefer full book, fall back to bookTicker spread) —
    def microstructure(self) -> dict:
        ob = self.order_book
        # spread from the freshest of bookTicker / ticker / book
        if self.book:
            bid, ask = self.book.bid_px, self.book.ask_px
        elif ob.best_bid and ob.best_ask:
            bid, ask = ob.best_bid, ob.best_ask
        elif self.ticker:
            bid, ask = self.ticker.best_bid, self.ticker.best_ask
        else:
            bid = ask = None
        out = {
            "bid": bid,
            "ask": ask,
            "mid": book_mid(bid, ask) if bid and ask else None,
            "spread": (ask - bid) if (bid and ask) else None,
            "spread_bps": spread_bps(bid, ask) if (bid and ask) else None,
            "depth_usd_10bps": ob.depth_usd(10.0) if ob.synced else None,
            "imbalance": ob.imbalance(10.0) if ob.synced else None,
            "slippage_bps_est": ob.slippage_bps_est(10000.0, "buy") if ob.synced else None,
            "book_synced": ob.synced,
        }
        return out

    def snapshot(self, max_age_ms: int, now_ms: Optional[int] = None,
                 chart_max_age_ms: Optional[int] = None) -> dict:
        """Full raw-vs-displayed view used by the API debug endpoint and /ws/live."""
        now_ms = now_ms if now_ms is not None else self._now_ms()
        chart_max = chart_max_age_ms if chart_max_age_ms is not None else max_age_ms
        bm = book_mid(self.book.bid_px, self.book.ask_px) if self.book else None
        raw = {
            "trade_price": self.trade.price if self.trade else None,
            "agg_trade_price": self.agg_trade.price if self.agg_trade else None,
            "ticker_last": self.ticker.last if self.ticker else None,
            "book_bid": self.book.bid_px if self.book else None,
            "book_bid_qty": self.book.bid_qty if self.book else None,
            "book_ask": self.book.ask_px if self.book else None,
            "book_ask_qty": self.book.ask_qty if self.book else None,
            "book_mid": bm,
            "kline_close": self.kline.close if self.kline else None,
        }
        ticker = None
        if self.ticker:
            t = self.ticker
            ticker = {
                "last": t.last, "price_change": t.price_change,
                "price_change_pct": t.price_change_pct, "weighted_avg": t.weighted_avg,
                "open": t.open, "high": t.high, "low": t.low,
                "volume_base": t.volume_base, "volume_quote": t.volume_quote,
                "num_trades": t.num_trades, "best_bid": t.best_bid, "best_ask": t.best_ask,
            }
        candle = None
        if self.kline:
            k = self.kline
            candle = {
                "time": k.start_ms // 1000, "open": k.open, "high": k.high,
                "low": k.low, "close": k.close, "value": k.volume_base,
                "interval": k.interval, "closed": k.is_closed,
            }
        return {
            "symbol": self.symbol,
            "native_symbol": self.native_symbol,
            "source": "binance_spot",
            "price_source": self.price_source,
            "displayed_price": self.displayed_price(),
            "feed_status": self.feed_status(max_age_ms, now_ms),
            "data_age_ms": self.staleness_ms(now_ms),
            "latency_ms": self.latency_ms(),
            "event_time": self.last_event_ms,
            "local_receive_time": self.last_recv_ms,
            "raw": raw,
            "ticker": ticker,
            "micro": self.microstructure(),
            "candle": candle,
            # Chart feed status, explicitly separate from the price feed above so the
            # cockpit can show CHART LIVE/STALE/NO CANDLES even while the price ticks.
            "chart_source": "binance_kline",
            "chart_status": self.chart_status(chart_max, now_ms),
            "candle_age_ms": self.candle_age_ms(now_ms),
            "kline_event_count": self.kline_event_count,
        }

    # — ingest (mutating, called by the hub on the event loop) —
    def _touch(self, ts_event_ms: Optional[int]) -> None:
        self.last_recv_ms = self._now_ms()
        if ts_event_ms:
            self.last_event_ms = max(self.last_event_ms or 0, int(ts_event_ms))

    def on_trade(self, ev: TradeEvent) -> None:
        self.trade = ev
        self._touch(ev.ts_event_ms)

    def on_agg_trade(self, ev: TradeEvent) -> None:
        self.agg_trade = ev
        self._touch(ev.ts_event_ms)

    def on_ticker(self, ev: TickerEvent) -> None:
        self.ticker = ev
        self._touch(ev.ts_event_ms)

    def on_book_ticker(self, ev: BookTickerEvent) -> None:
        self.book = ev
        self._touch(ev.ts_event_ms)

    def on_kline(self, ev: KlineEvent) -> None:
        self.kline = ev
        self.last_kline_recv_ms = self._now_ms()
        self.kline_event_count += 1
        self._touch(ev.ts_event_ms)


# ── Async hub (the only part that touches the network) ───────────────────────

def _http_get_json(url: str, params: dict, timeout: float = 8.0):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "antigravity-cockpit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted host)
        return json.loads(r.read().decode())


class BinanceSpotHub:
    """
    Owns one combined-stream WS for all configured symbols + REST snapshots.
    Read live state back via ``snapshot(symbol)`` / ``klines(symbol)``.
    Robust to disconnects (auto-reconnect with capped backoff). All failures
    degrade to honest "no data" — the hub never invents values.
    """

    def __init__(
        self,
        symbols: list[str],
        price_source: str = "trade",
        candle_interval: str = "1m",
        ws_base: str = "wss://stream.binance.com:9443",
        rest_base: str = "https://api.binance.com",
        depth_limit: int = 100,
        max_age_ms: int = 3000,
        kline_history: int = 500,
        chart_max_age_ms: int = 6000,
        max_candles: int = 1500,
        active_symbol_limit: int = 20,
        range_intervals: Optional[dict] = None,
        depth_only_selected: bool = True,
        depth_buffer_max: int = 2000,
    ) -> None:
        self.symbols = list(symbols)
        # The always-on full-detail set (Tier 3). The currently *selected* symbol is
        # added on demand and the oldest non-core dynamic slot is evicted, so the
        # full-detail stream set never exceeds ``active_symbol_limit``.
        self.core_symbols = list(symbols)
        self.active_symbol = symbols[0] if symbols else None
        self.active_symbol_limit = max(len(self.core_symbols), active_symbol_limit)
        self.price_source = price_source if price_source in PRICE_SOURCES else "trade"
        self.candle_interval = candle_interval
        self.range_intervals = dict(range_intervals or DEFAULT_RANGE_INTERVALS)
        self.ws_base = ws_base.rstrip("/")
        self.rest_base = rest_base.rstrip("/")
        self.depth_limit = depth_limit
        self.max_age_ms = max_age_ms
        self.kline_history = kline_history
        self.max_candles = max(kline_history, max_candles)
        # When True, maintain the full L2 order book (depth stream) ONLY for the
        # selected symbol — the other core symbols don't need their book for display.
        self.depth_only_selected = depth_only_selected
        # Hard cap on buffered depth diffs while the book is unsynced (bounded memory).
        self.depth_buffer_max = max(100, depth_buffer_max)
        # Chart counts as LIVE only if a kline arrived within this window. Klines
        # push ~every 2s for >=1m intervals (1s for the 1s interval), so the default
        # is looser than the price max_age_ms.
        self.chart_max_age_ms = chart_max_age_ms

        self.states: dict[str, SymbolState] = {}
        self._native_to_canonical: dict[str, str] = {}
        self._kline_cache: dict[str, list[dict]] = {}
        self._depth_buffer: dict[str, list[DepthUpdate]] = {}
        for s in self.symbols:
            self._register_state(s)

        self._kline_logged: set[str] = set()  # symbols that already logged their first kline (INFO once)
        self.connected: bool = False
        self.last_connect_ms: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._ws = None                       # live socket (closed on a symbol/interval switch)
        self._reconnect_requested: bool = False

        # best-effort metrics
        try:
            from metrics import (
                binance_live_connected, binance_live_events_total,
                binance_live_staleness_ms, binance_live_latency_ms,
                binance_book_resync_total,
            )
            self._m = {
                "connected": binance_live_connected,
                "events": binance_live_events_total,
                "staleness": binance_live_staleness_ms,
                "latency": binance_live_latency_ms,
                "resync": binance_book_resync_total,
            }
        except Exception:  # pragma: no cover
            self._m = {}

    # — symbol registration / dynamic active selection —
    def _register_state(self, symbol: str) -> None:
        if symbol in self.states:
            return
        native_up = to_upper_native(symbol)
        self.states[symbol] = SymbolState(symbol=symbol, native_symbol=native_up,
                                          price_source=self.price_source)
        self._native_to_canonical[native_up] = symbol
        self._kline_cache.setdefault(symbol, [])
        self._depth_buffer.setdefault(symbol, [])

    def _drop_state(self, symbol: str) -> None:
        if symbol in self.core_symbols:
            return
        st = self.states.pop(symbol, None)
        if st is not None:
            self._native_to_canonical.pop(st.native_symbol, None)
        self._kline_cache.pop(symbol, None)
        self._depth_buffer.pop(symbol, None)
        self._kline_logged.discard(symbol)

    # State-mutation helpers (no I/O). Public methods below add a single reconnect.
    def _apply_active_symbol(self, symbol: str) -> bool:
        """Make ``symbol`` the selected Tier-3 symbol. Returns True if the stream
        set changed (a reconnect is needed)."""
        self.active_symbol = symbol
        if symbol in self.states:
            return False
        # Evict oldest non-core dynamic symbols to stay within the limit.
        dynamic = [s for s in self.symbols if s not in self.core_symbols]
        while len(self.symbols) >= self.active_symbol_limit and dynamic:
            victim = dynamic.pop(0)
            self.symbols.remove(victim)
            self._drop_state(victim)
        self._register_state(symbol)
        if symbol not in self.symbols:
            self.symbols.append(symbol)
        return True

    def _apply_interval(self, interval: str) -> bool:
        """Switch the live kline interval. Returns True if it changed."""
        if interval not in VALID_INTERVALS or interval == self.candle_interval:
            return False
        self.candle_interval = interval
        # Clear cached candles AND the live kline state for every tracked symbol so
        # the chart reloads cleanly at the new interval (no mixing, and chart_status
        # honestly reports 'nodata' until a new-interval kline arrives).
        for s in list(self._kline_cache.keys()):
            self._kline_cache[s] = []
        for st in self.states.values():
            st.kline = None
            st.last_kline_recv_ms = None
            st.kline_event_count = 0
        self._kline_logged.clear()
        return True

    async def set_active_symbol(self, symbol: str) -> dict:
        """
        Make ``symbol`` the full-detail (Tier 3) selected symbol. If already tracked
        this is a no-op; otherwise it is added (evicting the oldest non-core slot at
        the limit) and the WS reconnects to (un)subscribe.
        """
        changed = self._apply_active_symbol(symbol)
        if changed:
            await self._trigger_reconnect()
        return {"symbol": symbol, "reconnect": changed, "tracked": list(self.symbols)}

    async def set_chart_interval(self, interval: str) -> dict:
        """Switch the live kline interval for the chart (drives range changes)."""
        if interval not in VALID_INTERVALS:
            return {"ok": False, "error": f"invalid interval {interval}",
                    "candle_interval": self.candle_interval}
        changed = self._apply_interval(interval)
        if changed:
            await self._trigger_reconnect()
        return {"ok": True, "changed": changed, "candle_interval": self.candle_interval}

    async def set_range(self, range_key: str) -> dict:
        """Convenience: map a UI range (1D/7D/1M/1Y, incl. 1J/7J/1An) → interval and apply it."""
        interval = range_to_interval(range_key, self.range_intervals)
        res = await self.set_chart_interval(interval)
        res["range"] = normalize_range(range_key)
        res["candle_interval"] = self.candle_interval
        return res

    async def set_active_and_range(self, symbol: str, range_key: str) -> dict:
        """Apply symbol + range together with a SINGLE reconnect (avoids the
        double-reconnect / double REST-init burst of calling both separately)."""
        a = self._apply_active_symbol(symbol)
        b = self._apply_interval(range_to_interval(range_key, self.range_intervals))
        if a or b:
            await self._trigger_reconnect()
        return {"symbol": symbol, "range": normalize_range(range_key),
                "candle_interval": self.candle_interval, "reconnect": (a or b),
                "tracked": list(self.symbols)}

    async def _trigger_reconnect(self) -> None:
        self._reconnect_requested = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover
                pass

    # — public API —
    def has_symbol(self, symbol: str) -> bool:
        return symbol in self.states

    def snapshot(self, symbol: str) -> Optional[dict]:
        st = self.states.get(symbol)
        if not st:
            return None
        snap = st.snapshot(self.max_age_ms, chart_max_age_ms=self.chart_max_age_ms)
        snap["candle_count"] = len(self._kline_cache.get(symbol, []))
        snap["candle_interval"] = self.candle_interval
        m = self._m.get("staleness")
        if m is not None and snap["data_age_ms"] is not None:
            m.labels(symbol=symbol).set(snap["data_age_ms"])
        return snap

    def klines(self, symbol: str) -> list[dict]:
        return list(self._kline_cache.get(symbol, []))

    async def fetch_klines_range(self, symbol: str, range_key: str) -> dict:
        """
        Fresh REST klines for a chart range (1D/7D/1M/1Y), at the configured
        interval for that range, capped at the Binance REST limit. Works for any
        valid Binance Spot symbol, tracked or not.
        """
        interval = range_to_interval(range_key, self.range_intervals)
        limit = klines_limit_for_range(range_key, interval, cap=min(1000, self.max_candles))
        try:
            rows = await asyncio.to_thread(
                _http_get_json, f"{self.rest_base}/api/v3/klines",
                {"symbol": to_upper_native(symbol), "interval": interval, "limit": limit},
            )
            return {"symbol": symbol, "range": normalize_range(range_key),
                    "interval": interval, "candles": parse_rest_klines(rows),
                    "source": "binance_kline"}
        except Exception as e:
            logger.warning("[binance_spot] range klines failed for %s %s: %s", symbol, range_key, e)
            return {"symbol": symbol, "range": normalize_range(range_key),
                    "interval": interval, "candles": [], "source": "binance_kline",
                    "error": str(e)}

    def status(self) -> dict:
        return {
            "enabled": True,
            "connected": self.connected,
            "price_source": self.price_source,
            "candle_interval": self.candle_interval,
            "max_age_ms": self.max_age_ms,
            "active_symbol": self.active_symbol,
            "core_symbols": list(self.core_symbols),
            "full_detail_symbols": list(self.symbols),
            "symbols": [
                {
                    "symbol": s,
                    "feed_status": st.feed_status(self.max_age_ms),
                    "displayed_price": st.displayed_price(),
                    "data_age_ms": st.staleness_ms(),
                }
                for s, st in self.states.items()
            ],
        }

    # — lifecycle —
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="binance_spot_hub")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass

    def _depth_symbols(self) -> list[str]:
        """Symbols that get the full L2 order book (depth stream). With
        ``depth_only_selected`` we only maintain the book for the selected symbol —
        the other core symbols don't need their book for display (memory/bandwidth)."""
        if self.depth_only_selected:
            return [self.active_symbol] if (self.active_symbol in self.states) else []
        return list(self.symbols)

    # — combined-stream URL —
    def _stream_url(self) -> str:
        streams = []
        depth_set = set(self._depth_symbols())
        for s in self.symbols:
            n = to_native(s)
            streams.append(f"{n}@trade")
            streams.append(f"{n}@aggTrade")
            streams.append(f"{n}@ticker")
            streams.append(f"{n}@bookTicker")
            streams.append(f"{n}@kline_{self.candle_interval}")
            if s in depth_set:
                streams.append(f"{n}@depth@100ms")
        return f"{self.ws_base}/stream?streams={'/'.join(streams)}"

    async def _run(self) -> None:
        import websockets  # local import keeps the module importable without the dep in tests
        backoff = 1.0
        while not self._stop.is_set():
            url = self._stream_url()
            try:
                logger.info("[binance_spot] connecting combined stream (%d symbols, interval=%s)",
                            len(self.symbols), self.candle_interval)
                self._reconnect_requested = False
                async with websockets.connect(url, ping_interval=15, ping_timeout=10, max_queue=2048) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_connect_ms = int(time.time() * 1000)
                    if self._m.get("connected"):
                        self._m["connected"].set(1)
                    backoff = 1.0
                    # REST init (klines history, book snapshot, 24h ticker) in the background
                    asyncio.create_task(self._rest_init())
                    async for raw in ws:
                        # Exit on stop OR a symbol/interval switch. Checking the flag
                        # here (not only ws.close()) closes the race where a switch
                        # lands during the handshake, while self._ws was still None.
                        if self._stop.is_set() or self._reconnect_requested:
                            break
                        try:
                            self._handle_message(raw)
                        except Exception as e:  # never let one bad frame kill the loop
                            logger.warning("[binance_spot] bad frame: %s", e)
            except asyncio.CancelledError:  # pragma: no cover
                break
            except Exception as e:
                logger.warning("[binance_spot] ws error: %s", e)
            finally:
                self._ws = None
                self.connected = False
                if self._m.get("connected"):
                    self._m["connected"].set(0)
            if self._stop.is_set():
                break
            # A user-triggered symbol/interval switch closed the socket on purpose —
            # reconnect immediately with the new stream set (no backoff sleep).
            if self._reconnect_requested:
                self._reconnect_requested = False
                continue
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    # — message dispatch —
    def _handle_message(self, raw: str) -> None:
        msg = json.loads(raw)
        # combined-stream envelope: {"stream": "...", "data": {...}}
        data = msg.get("data", msg)
        stream = msg.get("stream", "")
        etype = data.get("e")

        native = data.get("s")
        canonical = self._native_to_canonical.get(native) if native else None
        st = self.states.get(canonical) if canonical else None

        def count(kind: str):
            if self._m.get("events"):
                self._m["events"].labels(stream=kind).inc()

        if etype == "trade":
            ev = parse_trade(data)
            if st:
                st.on_trade(ev)
                self._observe_latency(st)
            count("trade")
        elif etype == "aggTrade":
            ev = parse_agg_trade(data)
            if st:
                st.on_agg_trade(ev)
            count("aggTrade")
        elif etype == "24hrTicker":
            ev = parse_ticker(data)
            if st:
                st.on_ticker(ev)
            count("ticker")
        elif etype == "kline":
            ev = parse_kline(data)
            if st:
                st.on_kline(ev)
                self._update_kline_cache(canonical, ev)
                self._log_kline(canonical, ev)
            count("kline")
        elif etype == "depthUpdate":
            ev = parse_depth_update(data)
            if st:
                self._handle_depth(canonical, st, ev)
            count("depth")
        elif "u" in data and "b" in data and "a" in data and "B" in data and "A" in data:
            # bookTicker (raw payload has no "e")
            ev = parse_book_ticker(data)
            if st:
                st.on_book_ticker(ev)
            count("bookTicker")

    def _observe_latency(self, st: SymbolState) -> None:
        if self._m.get("latency"):
            lat = st.latency_ms()
            if lat is not None:
                self._m["latency"].observe(lat)

    def _log_kline(self, canonical: str, ev: KlineEvent) -> None:
        """Explicit chart-feed evidence: first kline per symbol at INFO, the rest at DEBUG."""
        first = canonical not in self._kline_logged
        if first:
            self._kline_logged.add(canonical)
        lvl = logging.INFO if first else logging.DEBUG
        if logger.isEnabledFor(lvl):
            cache_n = len(self._kline_cache.get(canonical, []))
            logger.log(
                lvl,
                "[binance_spot] kline %s %s open_ms=%d close_ms=%d O=%s H=%s L=%s C=%s V=%s "
                "closed=%s recv_ms=%d candles=%d",
                canonical, ev.interval, ev.start_ms, ev.close_ms,
                ev.open, ev.high, ev.low, ev.close, ev.volume_base, ev.is_closed,
                int(time.time() * 1000), cache_n,
            )

    def _update_kline_cache(self, canonical: str, ev: KlineEvent) -> None:
        cache = self._kline_cache.setdefault(canonical, [])
        candle = {
            "time": ev.start_ms // 1000, "open": ev.open, "high": ev.high,
            "low": ev.low, "close": ev.close, "value": ev.volume_base,
        }
        if cache and cache[-1]["time"] == candle["time"]:
            cache[-1] = candle
        else:
            cache.append(candle)
            if len(cache) > self.max_candles:
                del cache[0:len(cache) - self.max_candles]

    def _handle_depth(self, canonical: str, st: SymbolState, ev: DepthUpdate) -> None:
        ob = st.order_book
        if not ob.synced:
            buf = self._depth_buffer.setdefault(canonical, [])
            buf.append(ev)
            # Bound the buffer: if the REST snapshot keeps failing, depth diffs (~10/s)
            # must not accumulate forever. Drop oldest beyond the cap.
            if len(buf) > self.depth_buffer_max:
                del buf[0:len(buf) - self.depth_buffer_max]
            return
        result = ob.apply_update(ev)
        if result == "gap":
            logger.info("[binance_spot] %s order-book gap, resyncing", canonical)
            if self._m.get("resync"):
                self._m["resync"].labels(symbol=canonical).inc()
            ob.synced = False
            self._depth_buffer[canonical] = [ev]
            asyncio.create_task(self._sync_order_book(canonical, st))

    # — REST init —
    async def _rest_init(self) -> None:
        depth_set = set(self._depth_symbols())
        for s in self.symbols:
            st = self.states.get(s)
            if st is None:
                continue
            await self._load_klines(s)
            # Only seed the order book for symbols that actually receive the depth
            # stream (memory: no book for non-selected symbols when depth_only_selected).
            if s in depth_set:
                await self._sync_order_book(s, st)
            await self._load_ticker(s)

    async def _load_klines(self, symbol: str) -> None:
        try:
            rows = await asyncio.to_thread(
                _http_get_json, f"{self.rest_base}/api/v3/klines",
                {"symbol": to_upper_native(symbol), "interval": self.candle_interval,
                 "limit": self.kline_history},
            )
            self._kline_cache[symbol] = parse_rest_klines(rows)
        except Exception as e:
            logger.warning("[binance_spot] klines init failed for %s: %s", symbol, e)

    async def _load_ticker(self, symbol: str) -> None:
        try:
            d = await asyncio.to_thread(
                _http_get_json, f"{self.rest_base}/api/v3/ticker/24hr",
                {"symbol": to_upper_native(symbol)},
            )
            # Only seed if the live stream has not already produced a ticker.
            st = self.states[symbol]
            if st.ticker is None:
                st.on_ticker(parse_ticker(d))
        except Exception as e:
            logger.warning("[binance_spot] 24h ticker init failed for %s: %s", symbol, e)

    async def _sync_order_book(self, symbol: str, st: SymbolState) -> None:
        """Snapshot → drop covered buffered events → apply in order (Binance procedure)."""
        try:
            snap = await asyncio.to_thread(
                _http_get_json, f"{self.rest_base}/api/v3/depth",
                {"symbol": to_upper_native(symbol), "limit": self.depth_limit},
            )
            ob = st.order_book
            ob.apply_snapshot(snap["lastUpdateId"], snap["bids"], snap["asks"])
            buffered = self._depth_buffer.get(symbol, [])
            self._depth_buffer[symbol] = []
            for ev in sorted(buffered, key=lambda e: e.final_update_id):
                res = ob.apply_update(ev)
                if res == "gap":
                    # snapshot was older than the buffer head — retry once shortly
                    ob.synced = False
                    await asyncio.sleep(0.5)
                    return await self._sync_order_book(symbol, st)
        except Exception as e:
            logger.warning("[binance_spot] depth snapshot failed for %s: %s", symbol, e)
            st.order_book.synced = False
            # Drop the (now stale) buffered diffs so memory stays bounded while REST
            # is failing, and schedule a delayed retry — otherwise synced stays False
            # forever while @depth events keep arriving.
            self._depth_buffer[symbol] = []
            if not self._stop.is_set():
                asyncio.create_task(self._retry_sync(symbol, st))

    async def _retry_sync(self, symbol: str, st: SymbolState, delay: float = 2.0) -> None:
        """Delayed order-book resync retry (one in flight per symbol via the
        not-synced guard) so a persistently failing REST depth call self-heals
        without spinning or leaking buffered diffs."""
        await asyncio.sleep(delay)
        if not self._stop.is_set() and not st.order_book.synced and symbol in self.states:
            await self._sync_order_book(symbol, st)
