"""
DeFi protocol tier (top protocols by TVL — DefiLlama, display-only)
==================================================================

A display-only, in-process layer that ranks the largest **DeFi protocols** by
total value locked and serves a light, bounded list to the cockpit. It is the
"DeFi / DEX par protocole" slice of the research report's expert dashboard, on
top of the macro DeFi-TVL aggregate already exposed by ``global_context.py``.

Tier separation (see CLAUDE.md): this is a **ranked list tier** (like the Binance
universe), distinct from
  * the macro tier (``global_context.py``) — whole-market scalars incl. *total*
    DeFi TVL from DefiLlama ``/v2/chains``
  * Tier 1/3 Binance hubs — CEX spot price/microstructure
It never feeds the bot or persistence — it only enriches the cockpit's context.

Source: DefiLlama ``/protocols`` (free, no API key, ToS-safe). One refresh yields
both the ranked protocol list **and** a TVL-by-category breakdown.

⚠️ Real-vs-noise: DefiLlama's ``/protocols`` is TVL-dominated by **CEX** reserves
(Binance/OKX/Bitfinex) and chain-level rows, which are *not* on-chain DeFi
protocols. Those categories are excluded by default (``NON_DEFI_CATEGORIES``) so
the panel shows genuine DeFi (Lido, Aave, EigenLayer…), not exchange balances.

Design split for offline testing (no network):
  * **Pure helpers** — ``is_defi_protocol`` / ``protocol_row`` / ``rank_protocols``
    / ``category_breakdown``. Zero I/O; fully unit-tested.
  * **DefiHub** — the only part that touches the network (periodic REST refresh).
    Read state back via ``protocols()`` / ``snapshot()`` / ``status()``.

Real data only: every row is a real DefiLlama value, explicitly sourced and
freshness-tracked. No data ⇒ empty list + honest ``connected=False`` — never a
fabricated protocol or TVL. A transient REST failure keeps the last good snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.request
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Categories in DefiLlama /protocols that are NOT genuine on-chain DeFi protocols.
# CEX = centralized-exchange reserves (Binance/OKX/…); Chain = chain-level rollups.
# Excluded by default so the panel ranks real DeFi, not exchange balances.
NON_DEFI_CATEGORIES: frozenset[str] = frozenset({"CEX", "Chain"})


def _f(x, default: Optional[float] = None) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ── Pure helpers (no I/O) ─────────────────────────────────────────────────────

def is_defi_protocol(category, exclude: frozenset[str] = NON_DEFI_CATEGORIES) -> bool:
    """True unless the category is an excluded non-DeFi bucket (CEX / Chain)."""
    return (category or "").strip() not in exclude


def protocol_row(p: dict, rank: Optional[int] = None) -> dict:
    """One DefiLlama protocol dict → a light, bounded cockpit row (real fields only)."""
    chains = p.get("chains") or []
    if not isinstance(chains, list):
        chains = []
    row = {
        "name": p.get("name") or p.get("slug") or "?",
        "symbol": (p.get("symbol") or "").upper() if p.get("symbol") not in (None, "-") else None,
        "category": p.get("category"),
        "chains": chains[:6],          # cap displayed chains (memory bound)
        "chains_count": len(chains),
        "tvl_usd": _f(p.get("tvl")),
        "change_1d": _f(p.get("change_1d")),
        "change_7d": _f(p.get("change_7d")),
        "mcap_usd": _f(p.get("mcap")),
        "url": p.get("url"),
        "slug": p.get("slug"),
        "source": "defillama",
    }
    if rank is not None:
        row["rank"] = rank
    return row


def rank_protocols(protocols: Iterable[dict], *, limit: int, min_tvl: float = 0.0,
                   exclude_categories: frozenset[str] = NON_DEFI_CATEGORIES) -> list[dict]:
    """Filter (DeFi-only, positive TVL ≥ floor) → sort by TVL desc → cap → rank. Pure."""
    kept = []
    for p in protocols:
        tvl = _f(p.get("tvl"))
        if tvl is None or tvl <= 0 or tvl < min_tvl:
            continue
        if not is_defi_protocol(p.get("category"), exclude_categories):
            continue
        kept.append((tvl, p))
    kept.sort(key=lambda t: t[0], reverse=True)
    return [protocol_row(p, rank=i + 1) for i, (_, p) in enumerate(kept[: max(0, limit)])]


def category_breakdown(protocols: Iterable[dict], *, top_n: int = 8,
                       min_tvl: float = 0.0,
                       exclude_categories: frozenset[str] = NON_DEFI_CATEGORIES) -> list[dict]:
    """TVL aggregated by category over the DeFi-only set → top-N categories. Pure."""
    agg: dict[str, dict] = {}
    for p in protocols:
        tvl = _f(p.get("tvl"))
        if tvl is None or tvl <= 0 or tvl < min_tvl:
            continue
        if not is_defi_protocol(p.get("category"), exclude_categories):
            continue
        cat = p.get("category") or "Other"
        slot = agg.setdefault(cat, {"category": cat, "tvl_usd": 0.0, "count": 0})
        slot["tvl_usd"] += tvl
        slot["count"] += 1
    rows = sorted(agg.values(), key=lambda r: r["tvl_usd"], reverse=True)
    return rows[: max(0, top_n)]


def total_tracked_tvl(protocols: Iterable[dict], *, min_tvl: float = 0.0,
                      exclude_categories: frozenset[str] = NON_DEFI_CATEGORIES) -> float:
    """Sum of TVL over the DeFi-only set above the floor (the panel's tracked total)."""
    total = 0.0
    for p in protocols:
        tvl = _f(p.get("tvl"))
        if tvl is None or tvl <= 0 or tvl < min_tvl:
            continue
        if is_defi_protocol(p.get("category"), exclude_categories):
            total += tvl
    return total


# ── HTTP helper (the network edge) ────────────────────────────────────────────

def _http_get_json(url: str, timeout: float = 10.0):
    # Identify the client toward DefiLlama (courtesy/transparency, matches rss_collector).
    req = urllib.request.Request(url, headers={
        "User-Agent": "antigravity-cockpit/1.0 (+https://github.com/GTpas/PortefeuilleCrypto)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted host)
        return json.loads(r.read().decode())


# ── Async hub (the only part that touches the network) ────────────────────────

class DefiHub:
    """
    Bounded ranking of the top DeFi protocols by TVL. Display-only.

    One background task re-polls DefiLlama ``/protocols`` every
    ``refresh_seconds`` and publishes (atomically) the ranked top-N + a
    TVL-by-category breakdown. Memory is bounded by ``limit`` (only the top-N
    rows are retained, never all ~7k protocols). Never opens a WebSocket, never
    persists, never feeds the bot.
    """

    def __init__(
        self,
        *,
        defillama_base: str = "https://api.llama.fi",
        limit: int = 50,
        min_tvl: float = 1_000_000.0,
        exclude_categories: Optional[Iterable[str]] = None,
        refresh_seconds: int = 120,
        http_timeout: float = 10.0,
        stale_ms: int = 600_000,
        category_top_n: int = 8,
    ) -> None:
        self.defillama_base = defillama_base.rstrip("/")
        self.limit = max(1, limit)
        self.min_tvl = max(0.0, min_tvl)
        self.exclude_categories = frozenset(exclude_categories) if exclude_categories else NON_DEFI_CATEGORIES
        self.refresh_seconds = max(30, refresh_seconds)
        self.http_timeout = http_timeout
        self.stale_ms = stale_ms
        self.category_top_n = category_top_n

        self._ranked: list[dict] = []        # last GOOD ranked rows (bounded by limit)
        self._categories: list[dict] = []     # last GOOD category breakdown
        self._total_tvl: Optional[float] = None
        self._raw_count: int = 0              # protocols seen in last good refresh
        self._eligible_count: int = 0         # DeFi-eligible protocols (post-filter, pre-cap)
        self.connected: bool = False
        self.last_refresh_ms: Optional[int] = None
        self._error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # — public read API —
    def protocols(self, limit: Optional[int] = None) -> list[dict]:
        rows = self._ranked
        return rows[:limit] if limit is not None else rows

    def _age_ms(self) -> Optional[int]:
        if not self.last_refresh_ms:
            return None
        return int(time.time() * 1000) - self.last_refresh_ms

    def snapshot(self, limit: Optional[int] = None) -> dict:
        age = self._age_ms()
        return {
            "enabled": True,
            "connected": self.connected,
            "real": bool(self._ranked),
            "count": len(self._ranked),
            "total_tracked_tvl_usd": self._total_tvl,
            "categories": self._categories,
            "protocols": self.protocols(limit),
            "min_tvl_usd": self.min_tvl,
            "excluded_categories": sorted(self.exclude_categories),
            "last_refresh_ms": self.last_refresh_ms,
            "age_ms": age,
            "stale": (age is not None and age > self.stale_ms),
            "error": self._error,
            "source": "defillama",
        }

    def status(self) -> dict:
        return {
            "enabled": True,
            "connected": self.connected,
            "count": len(self._ranked),
            "last_refresh_ms": self.last_refresh_ms,
            "source": "defillama",
        }

    # — lifecycle —
    async def start(self) -> None:
        asyncio.create_task(self._refresh())          # seed off the startup path
        self._task = asyncio.create_task(self._run(), name="defi_hub")

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
            await self._refresh()

    async def _refresh(self) -> None:
        from metrics import (defi_protocols_refresh_total, defi_protocols_refresh_errors_total,
                             defi_protocols_refresh_latency_ms, defi_protocols_loaded,
                             defi_tracked_tvl_usd)
        defi_protocols_refresh_total.inc()
        t0 = time.time()
        try:
            payload = await asyncio.to_thread(
                _http_get_json, f"{self.defillama_base}/protocols", self.http_timeout)
        except Exception as e:  # transient REST failure → keep last good snapshot
            defi_protocols_refresh_errors_total.inc()
            self._error = str(e)
            logger.warning("[defi] /protocols refresh failed (kept last snapshot): %s", e)
            return

        if not isinstance(payload, list) or not payload:
            defi_protocols_refresh_errors_total.inc()
            self._error = "empty_or_unparseable_response"
            logger.warning("[defi] /protocols returned no usable list (kept last snapshot)")
            return

        # Pre-filter ONCE to the DeFi-eligible subset (positive TVL ≥ floor, not
        # CEX/Chain) instead of three independent passes over the ~7.6k raw rows.
        eligible = [
            p for p in payload
            if is_defi_protocol(p.get("category"), self.exclude_categories)
            and (_f(p.get("tvl")) or 0.0) >= self.min_tvl and (_f(p.get("tvl")) or 0.0) > 0
        ]
        ranked = rank_protocols(eligible, limit=self.limit, min_tvl=self.min_tvl,
                                exclude_categories=self.exclude_categories)
        categories = category_breakdown(eligible, top_n=self.category_top_n,
                                        min_tvl=self.min_tvl, exclude_categories=self.exclude_categories)
        # Real-data-only: an empty DeFi set is NOT a "$0 TVL" reading. Publish None
        # (frontend shows n/a / "no protocols") rather than a fabricated zero total.
        total = total_tracked_tvl(eligible, min_tvl=self.min_tvl,
                                  exclude_categories=self.exclude_categories) if eligible else None
        # Atomic publish.
        self._ranked = ranked
        self._categories = categories
        self._total_tvl = total
        self._raw_count = len(payload)
        self._eligible_count = len(eligible)
        self.connected = True
        self.last_refresh_ms = int(time.time() * 1000)
        self._error = None
        latency_ms = (time.time() - t0) * 1000.0
        defi_protocols_refresh_latency_ms.observe(latency_ms)
        defi_protocols_loaded.set(len(ranked))
        if total is not None:
            defi_tracked_tvl_usd.set(total)
        logger.info("[defi] refreshed: %d ranked / %d eligible / %d raw protocols, tracked TVL %s in %.0fms",
                    len(ranked), len(eligible), len(payload),
                    f"${total:,.0f}" if total is not None else "n/a", latency_ms)
