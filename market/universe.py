"""
Market universe (Tier 1 — light, ≤300 trending symbols)
=======================================================

A display-only, in-process layer that ranks the most *trending* Binance **Spot**
pairs and serves a light snapshot to the cockpit. It exists so the watchlist can
show hundreds of symbols without opening hundreds of heavy streams.

Tier separation (see CLAUDE.md):
  * **Tier 1 — universe (this module):** ONE all-market ``!ticker@arr`` stream +
    a periodic ``/api/v3/ticker/24hr`` REST refresh feed an in-memory, **bounded**
    ranking (price / 24h change / quote volume / trade count). No trade/kline/depth
    streams, no order book — just enough to rank and display.
  * **Tier 3 — selected symbol:** the full-detail ``BinanceSpotHub`` (trade,
    aggTrade, ticker, bookTicker, kline, depth) for the symbol on the chart.

Design split for offline testing (no network):
  * **Pure helpers** — ``is_stablecoin`` / ``is_leverage_token`` /
    ``trending_score`` / ``rank_universe`` / ``parse_*_ticker``. Zero I/O.
  * **BinanceUniverseHub** — the only part that touches the network: the light WS
    + REST refresh. Read state back via ``universe()`` / ``get()`` / ``status()``.

Real data only: every row is real Binance Spot data, explicitly sourced and
freshness-tracked. If the hub has no data it returns an empty universe and an
honest ``connected=False`` status — it never fabricates a symbol or a price.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Pure stablecoin / fiat bases — when EXCLUDE_STABLES, a <STABLE>/USDT pair is a
# stable-to-stable pair and carries no trading signal, so it is dropped.
STABLE_BASES: frozenset[str] = frozenset({
    "USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "GUSD", "USDD",
    "PYUSD", "USTC", "UST", "FRAX", "LUSD", "SUSD", "USD1", "EURI", "AEUR",
    "EUR", "GBP", "AUD", "JPY", "TRY", "BRL", "ARS", "RUB", "ZAR", "NGN",
    "UAH", "IDRT", "BIDR", "VAI", "MXN", "PLN", "RON", "CZK", "COP",
})

# Leverage-token suffixes (Binance UP/DOWN/BULL/BEAR family + 3L/3S/5L/5S style).
_LEVERAGE_NUMERIC = re.compile(r"\d(?:L|S)$")
_LEVERAGE_WORDS = ("UP", "DOWN", "BULL", "BEAR")


def is_stablecoin(base: str) -> bool:
    return base.upper() in STABLE_BASES


def is_leverage_token(base: str) -> bool:
    """
    True for leverage tokens like ETH3L / BTC5S / SUSHIUP / BTCDOWN, while keeping
    genuine tokens that merely end in those letters (e.g. JUP — prefix "J" is too
    short to be a leverage wrapper). Conservative on purpose.
    """
    b = base.upper()
    if _LEVERAGE_NUMERIC.search(b):
        return True
    for suf in _LEVERAGE_WORDS:
        if b.endswith(suf) and (len(b) - len(suf)) >= 2:
            return True
    return False


def to_canonical(native: str, quote: str) -> Optional[str]:
    """``BTCUSDT`` + quote ``USDT`` → ``BTC/USDT``. None if it is not a <base><quote> pair."""
    q = quote.upper()
    n = native.upper()
    if not n.endswith(q) or len(n) <= len(q):
        return None
    return f"{n[:-len(q)]}/{q}"


def _f(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ── Light ticker (one per universe symbol) ───────────────────────────────────

@dataclass
class UniverseTicker:
    symbol: str          # canonical "BTC/USDT"
    base: str
    quote: str
    last: float
    open: float
    high: float
    low: float
    change_pct: float    # 24h
    quote_volume: float  # 24h quote-notional
    base_volume: float   # 24h base
    num_trades: int      # 24h
    best_bid: float
    best_ask: float
    ts_ms: int           # event/close time (ms)
    recv_ms: int         # local receive time (ms)

    def spread_bps(self) -> Optional[float]:
        if self.best_bid <= 0 or self.best_ask <= 0:
            return None
        mid = (self.best_bid + self.best_ask) / 2.0
        if mid <= 0:
            return None
        return (self.best_ask - self.best_bid) / mid * 10000.0

    def volatility_range(self) -> float:
        if self.last <= 0 or self.high <= 0 or self.low <= 0:
            return 0.0
        return max(0.0, (self.high - self.low) / self.last)

    def age_ms(self, now_ms: Optional[int] = None) -> Optional[float]:
        if not self.recv_ms:
            return None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return max(0.0, now_ms - self.recv_ms)

    def to_row(self, now_ms: Optional[int] = None, stale_ms: int = 15000) -> dict:
        age = self.age_ms(now_ms)
        return {
            "symbol": self.symbol,
            "base": self.base,
            "quote": self.quote,
            "price": self.last,
            "change_pct": self.change_pct,
            "quote_volume": self.quote_volume,
            "base_volume": self.base_volume,
            "num_trades": self.num_trades,
            "spread_bps": self.spread_bps(),
            "trending_score": round(trending_score(self), 6),
            "age_ms": age,
            "stale": (age is not None and age > stale_ms),
            "source": "binance_spot",
        }


# ── Parsers (pure) ───────────────────────────────────────────────────────────

def parse_rest_24h(d: dict, quote: str) -> Optional[UniverseTicker]:
    """One row of REST ``/api/v3/ticker/24hr`` → UniverseTicker (None if not <base><quote>)."""
    sym = to_canonical(d.get("symbol", ""), quote)
    if sym is None:
        return None
    base = sym.split("/")[0]
    now = int(time.time() * 1000)
    return UniverseTicker(
        symbol=sym, base=base, quote=quote.upper(),
        last=_f(d.get("lastPrice")), open=_f(d.get("openPrice")),
        high=_f(d.get("highPrice")), low=_f(d.get("lowPrice")),
        change_pct=_f(d.get("priceChangePercent")),
        quote_volume=_f(d.get("quoteVolume")), base_volume=_f(d.get("volume")),
        num_trades=int(_f(d.get("count"))),
        best_bid=_f(d.get("bidPrice")), best_ask=_f(d.get("askPrice")),
        ts_ms=int(_f(d.get("closeTime"), now)), recv_ms=now,
    )


def parse_arr_ticker(d: dict, quote: str) -> Optional[UniverseTicker]:
    """One element of the WS ``!ticker@arr`` array → UniverseTicker."""
    sym = to_canonical(d.get("s", ""), quote)
    if sym is None:
        return None
    base = sym.split("/")[0]
    now = int(time.time() * 1000)
    return UniverseTicker(
        symbol=sym, base=base, quote=quote.upper(),
        last=_f(d.get("c")), open=_f(d.get("o")), high=_f(d.get("h")), low=_f(d.get("l")),
        change_pct=_f(d.get("P")),
        quote_volume=_f(d.get("q")), base_volume=_f(d.get("v")),
        num_trades=int(_f(d.get("n"))),
        best_bid=_f(d.get("b")), best_ask=_f(d.get("a")),
        ts_ms=int(_f(d.get("E") or d.get("C"), now)), recv_ms=now,
    )


# ── Trending score (pure) ────────────────────────────────────────────────────
# A bounded composite in roughly [0, 1]. Constants are chosen so the dominant
# term is liquidity/activity (quote volume), then trade count, then move size,
# then realized range and spread quality. Only the RELATIVE ordering matters.

def _norm_log(x: float, ref_log10: float) -> float:
    if x <= 1:
        return 0.0
    return min(1.0, math.log10(x) / ref_log10)


def trending_score(t: UniverseTicker, now_ms: Optional[int] = None,
                   stale_ms: int = 15000) -> float:
    vol = _norm_log(t.quote_volume, 11.0)           # 1e11 quote vol → 1.0 (separates large caps)
    trades = _norm_log(float(t.num_trades), 6.5)    # ~3.2M trades → 1.0
    move = min(1.0, abs(t.change_pct) / 15.0)       # ±15% 24h move → 1.0
    rng = min(1.0, t.volatility_range() / 0.25)     # 25% H-L range → 1.0
    sp = t.spread_bps()
    spread_q = 1.0 - min(1.0, sp / 50.0) if sp is not None else 0.5  # ≤0bps→1, ≥50bps→0

    score = 0.45 * vol + 0.20 * trades + 0.20 * move + 0.10 * rng + 0.05 * spread_q

    age = t.age_ms(now_ms)
    if age is not None and age > stale_ms:
        score -= 0.10  # deprioritize stale rows, never drop silently
    return max(0.0, score)


# ── Filtering + ranking (pure) ───────────────────────────────────────────────

def passes_filters(t: UniverseTicker, *, exclude_stables: bool, exclude_leverage: bool,
                   min_quote_volume: float, valid_spot: Optional[set[str]] = None) -> bool:
    if valid_spot is not None and t.symbol not in valid_spot:
        return False
    if t.quote_volume < min_quote_volume:
        return False
    if t.last <= 0:
        return False
    if exclude_stables and is_stablecoin(t.base):
        return False
    if exclude_leverage and is_leverage_token(t.base):
        return False
    return True


def rank_universe(tickers: Iterable[UniverseTicker], *, limit: int,
                  exclude_stables: bool = True, exclude_leverage: bool = True,
                  min_quote_volume: float = 0.0,
                  valid_spot: Optional[set[str]] = None,
                  now_ms: Optional[int] = None, stale_ms: int = 15000) -> list[dict]:
    """Filter → score → sort desc → cap at ``limit`` → assign rank. Pure."""
    kept = [
        t for t in tickers
        if passes_filters(t, exclude_stables=exclude_stables, exclude_leverage=exclude_leverage,
                          min_quote_volume=min_quote_volume, valid_spot=valid_spot)
    ]
    kept.sort(key=lambda t: trending_score(t, now_ms, stale_ms), reverse=True)
    rows = []
    for i, t in enumerate(kept[: max(0, limit)]):
        row = t.to_row(now_ms, stale_ms)
        row["rank"] = i + 1
        rows.append(row)
    return rows


# ── Async hub (the only part that touches the network) ───────────────────────

def _http_get_json(url: str, params: dict, timeout: float = 12.0):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "antigravity-cockpit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted host)
        return json.loads(r.read().decode())


class BinanceUniverseHub:
    """
    Light, bounded universe of the top trending Binance Spot pairs.

    Membership (which top-N symbols) is recomputed from a periodic REST 24h
    snapshot; prices/volumes are kept fresh in between by the all-market
    ``!ticker@arr`` WS — but only for symbols currently in the universe, so the
    in-memory dict never exceeds ``max_symbols``.
    """

    def __init__(
        self,
        quote_asset: str = "USDT",
        limit: int = 300,
        min_quote_volume: float = 5_000_000.0,
        exclude_stables: bool = True,
        exclude_leverage: bool = True,
        refresh_seconds: int = 60,
        ws_base: str = "wss://stream.binance.com:9443",
        rest_base: str = "https://api.binance.com",
        max_symbols: int = 300,
        stale_ms: int = 15000,
    ) -> None:
        self.quote = quote_asset.upper()
        self.limit = min(limit, max_symbols)
        self.min_quote_volume = min_quote_volume
        self.exclude_stables = exclude_stables
        self.exclude_leverage = exclude_leverage
        self.refresh_seconds = max(10, refresh_seconds)
        self.ws_base = ws_base.rstrip("/")
        self.rest_base = rest_base.rstrip("/")
        self.max_symbols = max_symbols
        self.stale_ms = stale_ms

        self._tickers: dict[str, UniverseTicker] = {}   # canonical → live ticker (bounded)
        self._universe_set: set[str] = set()            # current top-N membership
        self._valid_spot: Optional[set[str]] = None     # SPOT/TRADING set from exchangeInfo
        self._ranked: list[dict] = []                   # cached ranked rows
        self.connected: bool = False
        self.last_refresh_ms: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # — public read API —
    def universe(self, limit: Optional[int] = None) -> list[dict]:
        rows = self._ranked
        if limit is not None:
            rows = rows[:limit]
        return rows

    def trending(self, limit: Optional[int] = None) -> list[dict]:
        # Already sorted by trending_score desc; alias kept for endpoint clarity.
        return self.universe(limit)

    def get(self, symbol: str) -> Optional[dict]:
        t = self._tickers.get(symbol)
        if not t:
            return None
        return t.to_row(stale_ms=self.stale_ms)

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self._tickers

    def status(self) -> dict:
        return {
            "enabled": True,
            "connected": self.connected,
            "quote": self.quote,
            "limit": self.limit,
            "count": len(self._ranked),
            "tracked": len(self._tickers),
            "min_quote_volume": self.min_quote_volume,
            "last_refresh_ms": self.last_refresh_ms,
            "source": "binance_spot",
        }

    # — lifecycle —
    async def start(self) -> None:
        # Seed concurrently (don't block API startup on Binance REST latency); the
        # WS loop only accumulates members once the first refresh lands.
        asyncio.create_task(self._refresh())
        self._task = asyncio.create_task(self._run(), name="binance_universe_hub")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass

    # — ranking from current state —
    def _recompute_ranked(self) -> None:
        self._ranked = rank_universe(
            self._tickers.values(), limit=self.limit,
            exclude_stables=self.exclude_stables, exclude_leverage=self.exclude_leverage,
            min_quote_volume=self.min_quote_volume, valid_spot=self._valid_spot,
            stale_ms=self.stale_ms,
        )

    # — REST refresh (membership + a fresh full snapshot) —
    async def _refresh(self) -> None:
        try:
            if self._valid_spot is None:
                self._valid_spot = await asyncio.to_thread(self._load_valid_spot)
            rows = await asyncio.to_thread(
                _http_get_json, f"{self.rest_base}/api/v3/ticker/24hr", {}
            )
        except Exception as e:
            logger.warning("[universe] REST refresh failed: %s", e)
            return

        candidates: list[UniverseTicker] = []
        for d in rows if isinstance(rows, list) else []:
            t = parse_rest_24h(d, self.quote)
            if t is None:
                continue
            if passes_filters(t, exclude_stables=self.exclude_stables,
                              exclude_leverage=self.exclude_leverage,
                              min_quote_volume=self.min_quote_volume,
                              valid_spot=self._valid_spot):
                candidates.append(t)

        ranked = rank_universe(
            candidates, limit=self.limit, exclude_stables=self.exclude_stables,
            exclude_leverage=self.exclude_leverage, min_quote_volume=self.min_quote_volume,
            valid_spot=self._valid_spot, stale_ms=self.stale_ms,
        )
        members = {r["symbol"] for r in ranked}
        # Rebuild the bounded ticker map from this snapshot (drops symbols that
        # left the top-N → memory stays bounded by limit, never grows unbounded).
        by_sym = {t.symbol: t for t in candidates}
        self._tickers = {s: by_sym[s] for s in members if s in by_sym}
        self._universe_set = members
        self._ranked = ranked
        self.last_refresh_ms = int(time.time() * 1000)
        logger.info("[universe] refreshed: %d symbols (of %d candidates)",
                    len(members), len(candidates))

    def _load_valid_spot(self) -> Optional[set[str]]:
        """SPOT + TRADING pairs for the configured quote, from exchangeInfo."""
        try:
            info = _http_get_json(f"{self.rest_base}/api/v3/exchangeInfo", {})
        except Exception as e:
            logger.warning("[universe] exchangeInfo failed (no spot filter): %s", e)
            return None
        out: set[str] = set()
        for s in info.get("symbols", []):
            if s.get("quoteAsset", "").upper() != self.quote:
                continue
            if s.get("status") != "TRADING":
                continue
            perms = s.get("permissions") or []
            if not (s.get("isSpotTradingAllowed") or "SPOT" in perms):
                continue
            out.add(f"{s.get('baseAsset', '').upper()}/{self.quote}")
        return out or None

    # — live WS (all-market light ticker) —
    def _stream_url(self) -> str:
        return f"{self.ws_base}/stream?streams=!ticker@arr"

    async def _run(self) -> None:
        import websockets  # local import keeps the module importable without the dep
        backoff = 1.0
        last_refresh = time.time()
        last_rerank = time.time()
        while not self._stop.is_set():
            try:
                logger.info("[universe] connecting all-market !ticker@arr")
                async with websockets.connect(self._stream_url(), ping_interval=15,
                                              ping_timeout=10, max_queue=64) as ws:
                    self.connected = True
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            self._handle_message(raw)
                        except Exception as e:  # never let one frame kill the loop
                            logger.debug("[universe] bad frame: %s", e)
                        now = time.time()
                        if now - last_refresh >= self.refresh_seconds:
                            last_refresh = now
                            asyncio.create_task(self._refresh())
                        elif now - last_rerank >= 2.0:
                            last_rerank = now
                            self._recompute_ranked()
            except asyncio.CancelledError:  # pragma: no cover
                break
            except Exception as e:
                logger.warning("[universe] ws error: %s", e)
            finally:
                self.connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    def _handle_message(self, raw: str) -> None:
        msg = json.loads(raw)
        data = msg.get("data", msg)
        if not isinstance(data, list):
            return
        # Update only symbols already in the universe (bounded memory). Membership
        # changes happen on the slower REST refresh, not from this stream.
        for d in data:
            sym = to_canonical(d.get("s", ""), self.quote)
            if sym is None or sym not in self._universe_set:
                continue
            t = parse_arr_ticker(d, self.quote)
            if t is not None:
                self._tickers[sym] = t
        # Hard memory guard (defensive — membership already bounds this).
        if len(self._tickers) > self.max_symbols:
            keep = set(self._universe_set)
            self._tickers = {s: v for s, v in self._tickers.items() if s in keep}
