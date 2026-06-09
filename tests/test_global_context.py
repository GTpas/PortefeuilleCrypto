"""
Offline tests for the global market-context hub (macro tier) — no network.

Covers the pure parsers (CoinGecko global, DefiLlama chains, Fear & Greed) and the
hub's real-data-only honesty: a source that has never answered reports real=False
with null values, a transient failure keeps the last good value, and the snapshot
never fabricates a macro number.
"""

import pytest

from market.global_context import (
    parse_coingecko_global, parse_defillama_chains, parse_fng, fng_band,
    GlobalContextHub, _Source,
)


# ── CoinGecko /global parser ──────────────────────────────────────────────────

def test_parse_coingecko_global_ok():
    payload = {"data": {
        "active_cryptocurrencies": 12000,
        "markets": 1100,
        "total_market_cap": {"usd": 2.5e12, "btc": 4.0e7},
        "total_volume": {"usd": 1.2e11},
        "market_cap_percentage": {"btc": 52.3, "eth": 17.1},
        "market_cap_change_percentage_24h_usd": 1.23,
        "updated_at": 1699999999,
    }}
    out = parse_coingecko_global(payload)
    assert out["total_market_cap_usd"] == 2.5e12
    assert out["total_volume_usd"] == 1.2e11
    assert out["btc_dominance"] == 52.3
    assert out["eth_dominance"] == 17.1
    assert out["market_cap_change_24h_pct"] == 1.23
    assert out["active_cryptocurrencies"] == 12000
    assert out["markets"] == 1100


def test_parse_coingecko_global_missing_mcap_is_none():
    # No total market cap → unusable; must NOT publish a zero/empty reading.
    assert parse_coingecko_global({"data": {"total_volume": {"usd": 1.0}}}) is None
    assert parse_coingecko_global({}) is None
    assert parse_coingecko_global({"data": {}}) is None


def test_parse_coingecko_handles_partial_dominance():
    payload = {"data": {"total_market_cap": {"usd": 1e12}, "market_cap_percentage": {"btc": 50.0}}}
    out = parse_coingecko_global(payload)
    assert out["btc_dominance"] == 50.0
    assert out["eth_dominance"] is None  # honest null, not 0


# ── DefiLlama /v2/chains parser ───────────────────────────────────────────────

def test_parse_defillama_chains_sums_tvl():
    payload = [
        {"name": "Ethereum", "tvl": 5.0e10},
        {"name": "Solana", "tvl": 1.0e10},
        {"name": "Tron", "tvl": 8.0e9},
    ]
    out = parse_defillama_chains(payload, top_n=2)
    assert out["defi_tvl_usd"] == pytest.approx(6.8e10)
    assert out["chains_count"] == 3
    assert [c["name"] for c in out["top_chains"]] == ["Ethereum", "Solana"]  # sorted desc, capped


def test_parse_defillama_skips_nonpositive_and_empty():
    assert parse_defillama_chains([]) is None
    assert parse_defillama_chains({"not": "a list"}) is None
    # All-zero/garbage tvl → no usable total → None (never publishes 0 TVL).
    assert parse_defillama_chains([{"name": "X", "tvl": 0}, {"name": "Y", "tvl": None}]) is None


# ── Fear & Greed parser ───────────────────────────────────────────────────────

def test_parse_fng_ok():
    payload = {"data": [{"value": "63", "value_classification": "Greed", "timestamp": "1699999999"}]}
    out = parse_fng(payload)
    assert out["value"] == 63.0
    assert out["classification"] == "Greed"
    assert out["timestamp"] == 1699999999


def test_parse_fng_empty_is_none():
    assert parse_fng({"data": []}) is None
    assert parse_fng({}) is None


def test_parse_fng_derives_band_when_label_missing():
    out = parse_fng({"data": [{"value": "10"}]})
    assert out["value"] == 10.0
    assert out["classification"] == "Extreme Fear"


def test_fng_band_boundaries():
    assert fng_band(None) == "unknown"
    assert fng_band(0) == "Extreme Fear"
    assert fng_band(24) == "Extreme Fear"
    assert fng_band(25) == "Fear"
    assert fng_band(50) == "Neutral"
    assert fng_band(60) == "Greed"
    assert fng_band(90) == "Extreme Greed"


# ── Hub honesty (real-data-only) ──────────────────────────────────────────────

def test_snapshot_empty_is_honest():
    """A fresh hub that has fetched nothing must report real=False with no values."""
    hub = GlobalContextHub()
    snap = hub.snapshot()
    assert snap["enabled"] is True
    for key in ("market", "defi", "sentiment"):
        block = snap[key]
        assert block["real"] is False
        assert "total_market_cap_usd" not in block  # no fabricated fields
    assert hub.status()["connected"] is False


def test_source_keeps_last_value_on_error():
    """A transient failure must not blank the last good value."""
    src = _Source("market", enabled=True, stale_ms=300_000)
    src.update_ok({"total_market_cap_usd": 1e12})
    src.update_err("boom")
    view = src.view("coingecko")
    assert view["real"] is True                      # still have the good value
    assert view["total_market_cap_usd"] == 1e12
    assert view["error"] == "boom"                   # but the error is surfaced


def test_source_view_marks_stale():
    src = _Source("defi", enabled=True, stale_ms=500)
    src.update_ok({"defi_tvl_usd": 1.0})
    src.last_ok_ms -= 2000   # backdate 2s → older than the 500ms stale window
    view = src.view("defillama")
    assert view["real"] is True
    assert view["stale"] is True


def test_disabled_source_not_real():
    hub = GlobalContextHub(enable_defillama=False)
    assert hub.defi.enabled is False
    snap = hub.snapshot()
    assert snap["defi"]["real"] is False
    assert snap["defi"]["enabled"] is False
