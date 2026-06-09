"""
Global market context (macro tier — total market cap, dominance, DeFi TVL, sentiment)
=====================================================================================

A display-only, in-process layer that gives the cockpit the *macro* backdrop the
Binance-only tiers cannot: whole-market capitalization, BTC/ETH dominance, 24h
global volume, total DeFi TVL, and a market sentiment gauge (Fear & Greed). It is
the "rapport crypto expert" macro section from the research report, scoped to the
free, no-API-key, ToS-safe public endpoints.

Tier separation (see CLAUDE.md): this is **macro**, distinct from
  * Tier 1 — universe (≤300 trending Binance Spot pairs)
  * Tier 3 — selected symbol (full-detail Binance Spot hub)
It never feeds the bot or persistence — it only enriches the cockpit's context.

Three independent free sources, each behind its own toggle:
  * CoinGecko ``/api/v3/global``      → total market cap / volume / dominance
  * DefiLlama  ``/v2/chains``         → total DeFi TVL (sum of per-chain TVL)
  * alternative.me ``/fng/``          → Fear & Greed sentiment index

Design split for offline testing (no network):
  * **Pure parsers** — ``parse_coingecko_global`` / ``parse_defillama_chains`` /
    ``parse_fng``. Zero I/O; fully unit-tested.
  * **GlobalContextHub** — the only part that touches the network: a periodic
    background poll. Read state back via ``snapshot()`` / ``status()``.

Real data only: every field is a real upstream value, explicitly sourced and
freshness-tracked. A source that has never answered (or is disabled) returns
``real=False`` with ``null`` values — the cockpit shows ``n/a``, never a
fabricated macro number.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


def _f(x, default: Optional[float] = None) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ── Pure parsers (no I/O) ─────────────────────────────────────────────────────

def parse_coingecko_global(payload: dict) -> Optional[dict]:
    """CoinGecko ``/api/v3/global`` → normalized macro dict (None if unusable).

    Real data only: returns None when the total market cap is absent, so the hub
    never publishes an empty/zero macro snapshot as if it were a reading.
    """
    data = (payload or {}).get("data", payload) or {}
    tmc = (data.get("total_market_cap") or {}).get("usd")
    total_market_cap = _f(tmc)
    if total_market_cap is None:
        return None
    dom = data.get("market_cap_percentage") or {}
    return {
        "total_market_cap_usd": total_market_cap,
        "total_volume_usd": _f((data.get("total_volume") or {}).get("usd")),
        "btc_dominance": _f(dom.get("btc")),
        "eth_dominance": _f(dom.get("eth")),
        "market_cap_change_24h_pct": _f(data.get("market_cap_change_percentage_24h_usd")),
        "active_cryptocurrencies": int(_f(data.get("active_cryptocurrencies"), 0) or 0),
        "markets": int(_f(data.get("markets"), 0) or 0),
        "updated_at": int(_f(data.get("updated_at"), 0) or 0),
    }


def parse_defillama_chains(payload, top_n: int = 5) -> Optional[dict]:
    """DefiLlama ``/v2/chains`` (list of per-chain TVL) → total DeFi TVL + top chains.

    None if the response is not a usable list, so a transient bad body never
    overwrites the last good TVL with zero.
    """
    if not isinstance(payload, list) or not payload:
        return None
    total = 0.0
    chains: list[dict] = []
    for c in payload:
        tvl = _f(c.get("tvl"), 0.0) or 0.0
        if tvl <= 0:
            continue
        total += tvl
        chains.append({"name": c.get("name") or c.get("gecko_id") or "?", "tvl_usd": tvl})
    if total <= 0:
        return None
    chains.sort(key=lambda x: x["tvl_usd"], reverse=True)
    return {
        "defi_tvl_usd": total,
        "chains_count": len(chains),
        "top_chains": chains[: max(0, top_n)],
    }


# Fear & Greed index value → a short, stable classification band. alternative.me
# already ships a classification string, but we derive our own band as a fallback
# so a missing/odd label never blanks the gauge.
def fng_band(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < 25:
        return "Extreme Fear"
    if value < 45:
        return "Fear"
    if value < 55:
        return "Neutral"
    if value < 75:
        return "Greed"
    return "Extreme Greed"


def parse_fng(payload: dict) -> Optional[dict]:
    """alternative.me ``/fng/`` → {value, classification, timestamp} (None if empty)."""
    rows = (payload or {}).get("data") or []
    if not rows:
        return None
    row = rows[0]
    value = _f(row.get("value"))
    if value is None:
        return None
    classification = row.get("value_classification") or fng_band(value)
    return {
        "value": value,
        "classification": classification,
        "timestamp": int(_f(row.get("timestamp"), 0) or 0),
    }


# ── HTTP helper (the network edge) ────────────────────────────────────────────

def _http_get_json(url: str, params: Optional[dict] = None, timeout: float = 10.0,
                   headers: Optional[dict] = None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    h = {"User-Agent": "antigravity-cockpit/1.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted hosts)
        return json.loads(r.read().decode())


# ── Per-source state (real-data-only freshness tracking) ──────────────────────

class _Source:
    """Holds the last GOOD value of one upstream + freshness/error, never fabricates."""

    def __init__(self, name: str, enabled: bool, stale_ms: int):
        self.name = name
        self.enabled = enabled
        self.stale_ms = stale_ms
        self.value: Optional[dict] = None      # last good parsed dict (or None)
        self.last_ok_ms: Optional[int] = None  # local recv ms of last good value
        self.error: Optional[str] = None       # last error string (cleared on success)

    def update_ok(self, value: dict) -> None:
        self.value = value
        self.last_ok_ms = int(time.time() * 1000)
        self.error = None

    def update_err(self, err: str) -> None:
        self.error = err  # keep last good value — never blank on a transient failure

    def view(self, source_label: str) -> dict:
        now = int(time.time() * 1000)
        age = (now - self.last_ok_ms) if self.last_ok_ms else None
        out = {
            "source": source_label,
            "enabled": self.enabled,
            "real": self.value is not None,
            "age_ms": age,
            "stale": (age is not None and age > self.stale_ms),
            "error": self.error,
        }
        if self.value:
            out.update(self.value)
        return out


# ── Async hub (the only part that touches the network) ────────────────────────

class GlobalContextHub:
    """
    Periodic background poll of the three free macro sources. Display-only.

    One ``asyncio`` task wakes every ``refresh_seconds`` and refreshes each enabled
    source independently (a failure in one never affects the others). Read state
    back via ``snapshot()``. Memory footprint is tiny and bounded (one small dict
    per source). The hub never opens a WebSocket and never persists anything.
    """

    def __init__(
        self,
        *,
        enable_coingecko: bool = True,
        enable_defillama: bool = True,
        enable_fear_greed: bool = True,
        coingecko_base: str = "https://api.coingecko.com/api/v3",
        coingecko_api_key: str = "",
        defillama_base: str = "https://api.llama.fi",
        fear_greed_base: str = "https://api.alternative.me",
        refresh_seconds: int = 60,
        http_timeout: float = 10.0,
        stale_ms: int = 300_000,
    ) -> None:
        self.refresh_seconds = max(15, refresh_seconds)
        self.http_timeout = http_timeout
        self.coingecko_base = coingecko_base.rstrip("/")
        self.coingecko_api_key = coingecko_api_key or ""
        self.defillama_base = defillama_base.rstrip("/")
        self.fear_greed_base = fear_greed_base.rstrip("/")

        self.market = _Source("market", enable_coingecko, stale_ms)
        self.defi = _Source("defi", enable_defillama, stale_ms)
        self.sentiment = _Source("sentiment", enable_fear_greed, stale_ms)

        self.last_refresh_ms: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # — public read API —
    def snapshot(self) -> dict:
        return {
            "enabled": True,
            "updated_at": self.last_refresh_ms,
            "refresh_seconds": self.refresh_seconds,
            "market": self.market.view("coingecko"),
            "defi": self.defi.view("defillama"),
            "sentiment": self.sentiment.view("fear_greed.alternative.me"),
        }

    def status(self) -> dict:
        return {
            "enabled": True,
            "connected": any(s.value is not None for s in (self.market, self.defi, self.sentiment)),
            "last_refresh_ms": self.last_refresh_ms,
            "sources": {
                "coingecko": self.market.enabled,
                "defillama": self.defi.enabled,
                "fear_greed": self.sentiment.enabled,
            },
            "source": "coingecko+defillama+alternative.me",
        }

    # — lifecycle —
    async def start(self) -> None:
        # Seed once immediately (off the API-startup critical path), then loop.
        asyncio.create_task(self._refresh_all())
        self._task = asyncio.create_task(self._run(), name="global_context_hub")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.refresh_seconds)
            except asyncio.CancelledError:  # pragma: no cover
                break
            if self._stop.is_set():
                break
            await self._refresh_all()

    async def _refresh_all(self) -> None:
        await asyncio.gather(
            self._refresh_source(self.market, "coingecko", self._fetch_coingecko),
            self._refresh_source(self.defi, "defillama", self._fetch_defillama),
            self._refresh_source(self.sentiment, "fear_greed", self._fetch_fng),
        )
        self.last_refresh_ms = int(time.time() * 1000)

    async def _refresh_source(self, src: _Source, metric_label: str, fetch) -> None:
        if not src.enabled:
            return
        from metrics import (global_context_refresh_total,
                             global_context_refresh_errors_total,
                             global_context_refresh_latency_ms)
        global_context_refresh_total.labels(source=metric_label).inc()
        t0 = time.time()
        try:
            parsed = await asyncio.to_thread(fetch)
            latency_ms = (time.time() - t0) * 1000.0
            global_context_refresh_latency_ms.labels(source=metric_label).observe(latency_ms)
            if parsed is None:
                global_context_refresh_errors_total.labels(source=metric_label).inc()
                src.update_err("empty_or_unparseable_response")
                return
            src.update_ok(parsed)
            self._export_gauges(metric_label, parsed)
        except Exception as e:  # transient REST failure → keep last good value
            global_context_refresh_errors_total.labels(source=metric_label).inc()
            src.update_err(str(e))
            logger.warning("[global_context] %s refresh failed (kept last value): %s", metric_label, e)

    def _export_gauges(self, metric_label: str, parsed: dict) -> None:
        from metrics import (global_total_market_cap_usd, global_btc_dominance_pct,
                             global_defi_tvl_usd, global_fear_greed_index)
        if metric_label == "coingecko":
            if parsed.get("total_market_cap_usd") is not None:
                global_total_market_cap_usd.set(parsed["total_market_cap_usd"])
            if parsed.get("btc_dominance") is not None:
                global_btc_dominance_pct.set(parsed["btc_dominance"])
        elif metric_label == "defillama" and parsed.get("defi_tvl_usd") is not None:
            global_defi_tvl_usd.set(parsed["defi_tvl_usd"])
        elif metric_label == "fear_greed" and parsed.get("value") is not None:
            global_fear_greed_index.set(parsed["value"])

    # — per-source fetchers (sync; run in a thread) —
    def _fetch_coingecko(self) -> Optional[dict]:
        headers = {}
        if self.coingecko_api_key:
            # Demo keys use x-cg-demo-api-key; Pro keys ignore it harmlessly.
            headers["x-cg-demo-api-key"] = self.coingecko_api_key
        payload = _http_get_json(f"{self.coingecko_base}/global", timeout=self.http_timeout, headers=headers)
        return parse_coingecko_global(payload)

    def _fetch_defillama(self) -> Optional[dict]:
        payload = _http_get_json(f"{self.defillama_base}/v2/chains", timeout=self.http_timeout)
        return parse_defillama_chains(payload)

    def _fetch_fng(self) -> Optional[dict]:
        payload = _http_get_json(f"{self.fear_greed_base}/fng/", params={"limit": 1}, timeout=self.http_timeout)
        return parse_fng(payload)
