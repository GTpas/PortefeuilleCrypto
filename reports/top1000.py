"""
Daily report — TOP-1000 external watchlist (CoinGecko markets)
==============================================================

Extends the daily report beyond the ~300 Binance-USDT universe: the top-1000
coins by market cap from CoinGecko ``/coins/markets`` (free, keyless tier) are
classified against the in-app universe so the report can separate:

* ``tracked``            — top-1000 coins already covered by the app universe;
* ``untracked``          — in the top 1000 but not followed by the app;
* ``new_opportunities``  — untracked coins liquid + active enough to be worth
                           adding to the universe (real volume floor);
* ``excluded``           — untracked coins rejected for an explicit reason
                           (volume too low / missing data).

Classification (``parse_markets_payload`` / ``classify``) is PURE and tested
offline. The fetch is best-effort and asynchronous-friendly (the caller wraps
it in a thread): 4 REST pages of 250, once per day at report time + on manual
generation — far inside CoinGecko's public rate limits. Real data only: if the
fetch fails the report carries ``status: unavailable`` + the error, never a
fabricated list.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

PER_PAGE = 250
DEFAULT_PAGES = 4               # 4 × 250 = top 1000
NEW_OPPORTUNITY_MAX = 30        # cap of the suggested-additions list


# ──────────────────────────────────────────────────────────────────────────────
# Pure parsing / classification
# ──────────────────────────────────────────────────────────────────────────────

def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def parse_markets_payload(payload) -> list[dict]:
    """One CoinGecko ``/coins/markets`` page → slim real rows. Unusable entries
    are dropped (never guessed)."""
    if not isinstance(payload, list):
        return []
    out = []
    for it in payload:
        if not isinstance(it, dict):
            continue
        sym = (it.get("symbol") or "").upper()
        if not sym:
            continue
        out.append({
            "base": sym,
            "name": it.get("name") or sym,
            "coingecko_id": it.get("id"),
            "market_cap_rank": it.get("market_cap_rank"),
            "market_cap": _f(it.get("market_cap")),
            "price": _f(it.get("current_price")),
            "change_24h": _f(it.get("price_change_percentage_24h")),
            "volume_24h": _f(it.get("total_volume")),
        })
    return out


def classify(cg_rows: list[dict], universe_bases: set[str], *,
             min_volume_usd: float, max_new: int = NEW_OPPORTUNITY_MAX) -> dict:
    """Split top-1000 rows against the app universe (pure, deterministic).

    ``universe_bases``: upper-case base assets currently in the app universe.
    New opportunities = untracked + real 24h volume ≥ floor + has a price,
    ranked by 24h volume (the most tradable first). Excluded rows carry the
    first failed reason (no double counting)."""
    tracked, untracked, excluded = [], [], []
    for r in cg_rows:
        if r["base"] in universe_bases:
            tracked.append(r)
            continue
        reason = None
        if r.get("price") is None or r.get("volume_24h") is None:
            reason = "donnees_insuffisantes"
        elif r["volume_24h"] < min_volume_usd:
            reason = "volume_trop_faible"
        if reason:
            excluded.append({**r, "exclusion_reason": reason})
        else:
            untracked.append(r)
    untracked.sort(key=lambda r: r.get("volume_24h") or 0.0, reverse=True)
    return {
        "tracked_count": len(tracked),
        "untracked_count": len(untracked) + len(excluded),
        "new_opportunities": untracked[:max_new],
        "excluded_count": len(excluded),
        "excluded_examples": excluded[:10],
        "min_volume_usd": min_volume_usd,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Best-effort fetch (sync; callers wrap in a thread / to_thread)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_top_markets(api_base: str, *, pages: int = DEFAULT_PAGES,
                      timeout: float = 10.0, api_key: str = "",
                      sleep_between_pages_s: float = 1.5) -> tuple[list[dict], Optional[str]]:
    """Fetch up to ``pages`` × 250 market rows. Returns (rows, error).

    Partial success is kept (better 500 real rows than none); the first error
    is reported so the report can disclose incompleteness honestly."""
    rows: list[dict] = []
    error: Optional[str] = None
    for page in range(1, max(1, pages) + 1):
        params = urllib.parse.urlencode({
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": PER_PAGE, "page": page, "sparkline": "false",
        })
        url = f"{api_base.rstrip('/')}/coins/markets?{params}"
        headers = {"User-Agent": "antigravity-report/1.0"}
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                payload = json.loads(r.read().decode())
            parsed = parse_markets_payload(payload)
            if not parsed:
                error = error or f"page {page}: empty/unusable payload"
                break
            rows.extend(parsed)
        except Exception as e:  # network/ratelimit → keep what we have
            error = error or f"page {page}: {e}"
            logger.warning("[top1000] fetch page %d failed: %s", page, e)
            break
        if page < pages:
            time.sleep(max(0.0, sleep_between_pages_s))  # public-tier politeness
    return rows, error


def build_external_watchlist(api_base: str, universe_bases: set[str], *,
                             enabled: bool, min_volume_usd: float,
                             pages: int = DEFAULT_PAGES, timeout: float = 10.0,
                             api_key: str = "") -> dict:
    """Full block for the report. Honest statuses: ``disabled`` /
    ``unavailable`` (with error) / ``partial`` / ``ok``."""
    if not enabled:
        return {"status": "disabled", "source": "coingecko",
                "reason": "ENABLE_TOP1000_WATCHLIST=False"}
    rows, error = fetch_top_markets(api_base, pages=pages, timeout=timeout, api_key=api_key)
    if not rows:
        return {"status": "unavailable", "source": "coingecko",
                "error": error or "no data", "rows_fetched": 0}
    block = classify(rows, universe_bases, min_volume_usd=min_volume_usd)
    block.update({
        "status": "partial" if error else "ok",
        "source": "coingecko /coins/markets",
        "rows_fetched": len(rows),
        "error": error,
    })
    return block
