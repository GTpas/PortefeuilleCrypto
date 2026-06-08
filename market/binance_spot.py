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

    def snapshot(self, max_age_ms: int, now_ms: Optional[int] = None) -> dict:
        """Full raw-vs-displayed view used by the API debug endpoint and /ws/live."""
        now_ms = now_ms if now_ms is not None else self._now_ms()
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
    ) -> None:
        self.symbols = symbols
        self.price_source = price_source if price_source in PRICE_SOURCES else "trade"
        self.candle_interval = candle_interval
        self.ws_base = ws_base.rstrip("/")
        self.rest_base = rest_base.rstrip("/")
        self.depth_limit = depth_limit
        self.max_age_ms = max_age_ms
        self.kline_history = kline_history

        self.states: dict[str, SymbolState] = {}
        self._native_to_canonical: dict[str, str] = {}
        for s in symbols:
            native_up = to_upper_native(s)
            self.states[s] = SymbolState(symbol=s, native_symbol=native_up, price_source=self.price_source)
            self._native_to_canonical[native_up] = s

        self._kline_cache: dict[str, list[dict]] = {}
        self._depth_buffer: dict[str, list[DepthUpdate]] = {s: [] for s in symbols}
        self.connected: bool = False
        self.last_connect_ms: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

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

    # — public API —
    def has_symbol(self, symbol: str) -> bool:
        return symbol in self.states

    def snapshot(self, symbol: str) -> Optional[dict]:
        st = self.states.get(symbol)
        if not st:
            return None
        snap = st.snapshot(self.max_age_ms)
        m = self._m.get("staleness")
        if m is not None and snap["data_age_ms"] is not None:
            m.labels(symbol=symbol).set(snap["data_age_ms"])
        return snap

    def klines(self, symbol: str) -> list[dict]:
        return list(self._kline_cache.get(symbol, []))

    def status(self) -> dict:
        return {
            "enabled": True,
            "connected": self.connected,
            "price_source": self.price_source,
            "candle_interval": self.candle_interval,
            "max_age_ms": self.max_age_ms,
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

    # — combined-stream URL —
    def _stream_url(self) -> str:
        streams = []
        for s in self.symbols:
            n = to_native(s)
            streams.append(f"{n}@trade")
            streams.append(f"{n}@aggTrade")
            streams.append(f"{n}@ticker")
            streams.append(f"{n}@bookTicker")
            streams.append(f"{n}@kline_{self.candle_interval}")
            streams.append(f"{n}@depth@100ms")
        return f"{self.ws_base}/stream?streams={'/'.join(streams)}"

    async def _run(self) -> None:
        import websockets  # local import keeps the module importable without the dep in tests
        backoff = 1.0
        while not self._stop.is_set():
            url = self._stream_url()
            try:
                logger.info("[binance_spot] connecting combined stream (%d symbols)", len(self.symbols))
                async with websockets.connect(url, ping_interval=15, ping_timeout=10, max_queue=2048) as ws:
                    self.connected = True
                    self.last_connect_ms = int(time.time() * 1000)
                    if self._m.get("connected"):
                        self._m["connected"].set(1)
                    backoff = 1.0
                    # REST init (klines history, book snapshot, 24h ticker) in the background
                    asyncio.create_task(self._rest_init())
                    async for raw in ws:
                        if self._stop.is_set():
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
                self.connected = False
                if self._m.get("connected"):
                    self._m["connected"].set(0)
            if self._stop.is_set():
                break
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
            if len(cache) > self.kline_history + 50:
                del cache[0:len(cache) - (self.kline_history + 50)]

    def _handle_depth(self, canonical: str, st: SymbolState, ev: DepthUpdate) -> None:
        ob = st.order_book
        if not ob.synced:
            self._depth_buffer[canonical].append(ev)
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
        for s in self.symbols:
            st = self.states[s]
            await self._load_klines(s)
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
