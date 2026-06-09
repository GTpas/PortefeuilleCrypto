"""
Offline tests for the DeFi protocol tier (top protocols by TVL) — no network.

Covers the parts that decide which protocols show up and in what order:
  * CEX / Chain exclusion (DefiLlama's TVL leaders are exchange reserves, not DeFi)
  * TVL floor + positive-TVL filtering, sort desc, cap, rank index
  * category breakdown + tracked-TVL total
  * hub honesty: empty snapshot reports real=False, no fabricated protocol
"""

import asyncio

import pytest

import market.defi as defi_mod
from market.defi import (
    is_defi_protocol, protocol_row, rank_protocols, category_breakdown,
    total_tracked_tvl, NON_DEFI_CATEGORIES, DefiHub,
)


def mk(name, tvl, category="Lending", chains=None, c1d=1.0, c7d=2.0, symbol="TKN", mcap=None):
    return {"name": name, "tvl": tvl, "category": category,
            "chains": chains if chains is not None else ["Ethereum"],
            "change_1d": c1d, "change_7d": c7d, "symbol": symbol, "mcap": mcap,
            "slug": name.lower(), "url": f"https://{name.lower()}.fi"}


# ── Exclusion ─────────────────────────────────────────────────────────────────

def test_is_defi_protocol_excludes_cex_and_chain():
    assert is_defi_protocol("Lending")
    assert is_defi_protocol("Liquid Staking")
    assert is_defi_protocol("")          # unknown/empty kept (not a known non-DeFi bucket)
    assert not is_defi_protocol("CEX")
    assert not is_defi_protocol("Chain")
    assert "CEX" in NON_DEFI_CATEGORIES and "Chain" in NON_DEFI_CATEGORIES


def test_rank_excludes_cex_reserves():
    # CEX dominates raw TVL but must never appear in a DeFi ranking.
    protocols = [
        mk("Binance CEX", 138e9, category="CEX"),
        mk("OKX", 22e9, category="CEX"),
        mk("Lido", 14e9, category="Liquid Staking"),
        mk("Aave", 10e9, category="Lending"),
    ]
    rows = rank_protocols(protocols, limit=10)
    names = [r["name"] for r in rows]
    assert names == ["Lido", "Aave"]          # CEX dropped, sorted by TVL desc
    assert rows[0]["rank"] == 1 and rows[1]["rank"] == 2


# ── Filtering / ranking ───────────────────────────────────────────────────────

def test_rank_applies_tvl_floor_and_sorts_and_caps():
    protocols = [
        mk("Big", 9e9), mk("Mid", 3e9), mk("Small", 5e5),  # Small below 1M floor
        mk("Zero", 0), mk("Neg", -5),                       # non-positive dropped
    ]
    rows = rank_protocols(protocols, limit=2, min_tvl=1_000_000.0)
    assert [r["name"] for r in rows] == ["Big", "Mid"]      # Small/Zero/Neg gone, capped at 2
    assert all("rank" in r for r in rows)


def test_rank_handles_missing_or_bad_tvl():
    protocols = [mk("Good", 2e9), {"name": "NoTvl", "category": "Lending"},
                 {"name": "BadTvl", "tvl": "oops", "category": "Lending"}]
    rows = rank_protocols(protocols, limit=10, min_tvl=0.0)
    assert [r["name"] for r in rows] == ["Good"]            # only the parseable positive TVL


def test_protocol_row_shape_and_chain_cap():
    p = mk("Multi", 1e9, chains=[f"c{i}" for i in range(12)], symbol="multi")
    row = protocol_row(p, rank=3)
    assert row["rank"] == 3
    assert row["chains_count"] == 12
    assert len(row["chains"]) == 6                          # displayed chains capped
    assert row["symbol"] == "MULTI"                         # uppercased
    assert row["source"] == "defillama"


def test_protocol_row_blank_symbol_is_none():
    assert protocol_row({"name": "X", "tvl": 1, "symbol": "-"})["symbol"] is None
    assert protocol_row({"name": "X", "tvl": 1})["symbol"] is None


# ── Category breakdown + total ────────────────────────────────────────────────

def test_category_breakdown_aggregates_and_excludes_cex():
    protocols = [
        mk("Binance CEX", 100e9, category="CEX"),  # excluded from breakdown
        mk("Lido", 14e9, category="Liquid Staking"),
        mk("RocketPool", 3e9, category="Liquid Staking"),
        mk("Aave", 10e9, category="Lending"),
    ]
    cats = category_breakdown(protocols, top_n=5)
    by = {c["category"]: c for c in cats}
    assert "CEX" not in by
    assert by["Liquid Staking"]["tvl_usd"] == pytest.approx(17e9)
    assert by["Liquid Staking"]["count"] == 2
    assert cats[0]["category"] == "Liquid Staking"          # sorted by TVL desc


def test_total_tracked_tvl_excludes_cex_and_floor():
    protocols = [mk("CEXr", 100e9, category="CEX"), mk("A", 5e9), mk("Tiny", 1e5)]
    # CEX excluded, Tiny below default floor when applied
    assert total_tracked_tvl(protocols, min_tvl=1_000_000.0) == pytest.approx(5e9)


# ── Hub honesty (real-data-only) ──────────────────────────────────────────────

def test_hub_empty_snapshot_is_honest():
    hub = DefiHub()
    snap = hub.snapshot()
    assert snap["enabled"] is True
    assert snap["connected"] is False
    assert snap["real"] is False
    assert snap["count"] == 0
    assert snap["protocols"] == []
    assert snap["total_tracked_tvl_usd"] is None            # no fabricated TVL
    assert hub.status()["connected"] is False


def test_hub_snapshot_limit_caps_protocols():
    hub = DefiHub(limit=50)
    hub._ranked = [protocol_row(mk(f"P{i}", (50 - i) * 1e9), rank=i + 1) for i in range(50)]
    hub.connected = True
    assert len(hub.snapshot(5)["protocols"]) == 5
    assert len(hub.snapshot()["protocols"]) == 50


def test_hub_uses_configured_exclusions():
    hub = DefiHub(exclude_categories=["CEX", "Chain", "Bridge"])
    assert "Bridge" in hub.exclude_categories


# ── _refresh honesty: empty eligible set must NOT publish a fabricated $0 TVL ──

def test_refresh_all_cex_publishes_null_total(monkeypatch):
    """A successful REST call that yields zero DeFi protocols (all CEX) must report
    real=False with total_tracked_tvl_usd=None — never a fabricated 0.0 reading."""
    hub = DefiHub(min_tvl=0.0)
    monkeypatch.setattr(defi_mod, "_http_get_json", lambda url, timeout=10.0: [
        {"name": "Binance CEX", "tvl": 1e11, "category": "CEX", "chains": ["BSC"]},
        {"name": "OKX", "tvl": 2e10, "category": "CEX", "chains": ["X"]},
    ])
    asyncio.run(hub._refresh())
    snap = hub.snapshot()
    assert snap["connected"] is True          # REST succeeded
    assert snap["real"] is False              # but no real DeFi
    assert snap["count"] == 0
    assert snap["protocols"] == []
    assert snap["total_tracked_tvl_usd"] is None   # NOT 0.0 (real-data-only rule)


def test_refresh_with_defi_sets_real_total(monkeypatch):
    """A real DeFi protocol present → real=True, count>0, total excludes CEX."""
    hub = DefiHub(min_tvl=0.0, limit=5)
    monkeypatch.setattr(defi_mod, "_http_get_json", lambda url, timeout=10.0: [
        {"name": "Lido", "tvl": 14e9, "category": "Liquid Staking", "chains": ["Ethereum"]},
        {"name": "Binance CEX", "tvl": 1e11, "category": "CEX", "chains": ["BSC"]},
    ])
    asyncio.run(hub._refresh())
    snap = hub.snapshot()
    assert snap["real"] is True and snap["count"] == 1
    assert snap["protocols"][0]["name"] == "Lido"
    assert snap["total_tracked_tvl_usd"] == pytest.approx(14e9)   # CEX excluded from total


def test_refresh_transient_failure_keeps_last_snapshot(monkeypatch):
    """A REST exception after a good refresh keeps the last good data + surfaces error."""
    hub = DefiHub(min_tvl=0.0, limit=5)
    monkeypatch.setattr(defi_mod, "_http_get_json", lambda url, timeout=10.0: [
        {"name": "Aave", "tvl": 10e9, "category": "Lending", "chains": ["Ethereum"]},
    ])
    asyncio.run(hub._refresh())
    assert hub.snapshot()["count"] == 1

    def boom(url, timeout=10.0):
        raise ConnectionError("defillama down")
    monkeypatch.setattr(defi_mod, "_http_get_json", boom)
    asyncio.run(hub._refresh())
    snap = hub.snapshot()
    assert snap["count"] == 1                       # last good snapshot preserved
    assert snap["total_tracked_tvl_usd"] == pytest.approx(10e9)
    assert snap["error"] == "defillama down"        # error surfaced, not hidden
