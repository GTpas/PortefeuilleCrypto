"""
Offline tests for the TOP-1000 external watchlist (pure parsing/classification —
no network; the fetch path is exercised only through its honest-failure branch).
"""

from __future__ import annotations

from reports import top1000


def _coin(sym, *, rank=1, mcap=1e9, price=10.0, chg=2.0, vol=5e7, name=None):
    return {"id": sym.lower(), "symbol": sym.lower(), "name": name or sym,
            "market_cap_rank": rank, "market_cap": mcap, "current_price": price,
            "price_change_percentage_24h": chg, "total_volume": vol}


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parse_markets_payload_real_rows_only():
    rows = top1000.parse_markets_payload([
        _coin("BTC", rank=1), _coin("ETH", rank=2),
        {"symbol": "", "name": "broken"},          # no symbol → dropped
        "garbage", None,                            # non-dict → dropped
    ])
    assert [r["base"] for r in rows] == ["BTC", "ETH"]
    assert rows[0]["market_cap_rank"] == 1
    assert rows[0]["price"] == 10.0


def test_parse_markets_payload_handles_bad_payload():
    assert top1000.parse_markets_payload(None) == []
    assert top1000.parse_markets_payload({"error": "rate limited"}) == []
    # missing numeric fields → honest None, never fabricated
    rows = top1000.parse_markets_payload([{"symbol": "abc", "name": "ABC"}])
    assert rows[0]["price"] is None and rows[0]["volume_24h"] is None


# ── classification ────────────────────────────────────────────────────────────

def test_classify_separates_tracked_untracked_excluded():
    cg = top1000.parse_markets_payload([
        _coin("BTC", vol=1e10), _coin("NEW", rank=120, vol=5e7),
        _coin("DUST", rank=900, vol=1e4),                 # below volume floor
        {"symbol": "nodata", "name": "NoData", "id": "x",  # missing price/volume
         "market_cap_rank": 500},
    ])
    out = top1000.classify(cg, {"BTC"}, min_volume_usd=1_000_000)
    assert out["tracked_count"] == 1
    assert [r["base"] for r in out["new_opportunities"]] == ["NEW"]
    assert out["excluded_count"] == 2
    reasons = {r["base"]: r["exclusion_reason"] for r in out["excluded_examples"]}
    assert reasons["DUST"] == "volume_trop_faible"
    assert reasons["NODATA"] == "donnees_insuffisantes"


def test_classify_orders_new_opportunities_by_volume_and_caps():
    cg = top1000.parse_markets_payload(
        [_coin(f"C{i}", rank=i + 10, vol=1e6 * (i + 1)) for i in range(40)])
    out = top1000.classify(cg, set(), min_volume_usd=1_000_000, max_new=5)
    opps = out["new_opportunities"]
    assert len(opps) == 5
    vols = [o["volume_24h"] for o in opps]
    assert vols == sorted(vols, reverse=True)   # most tradable first


# ── honest statuses (no network in tests) ─────────────────────────────────────

def test_build_external_watchlist_disabled_is_explicit():
    out = top1000.build_external_watchlist("http://unused", set(), enabled=False,
                                           min_volume_usd=1e6)
    assert out["status"] == "disabled"
    assert "ENABLE_TOP1000_WATCHLIST" in out["reason"]


def test_build_external_watchlist_unreachable_is_unavailable():
    # Invalid scheme → urllib fails fast; the block must be honest, never fabricated.
    out = top1000.build_external_watchlist("invalid://nowhere", set(), enabled=True,
                                           min_volume_usd=1e6, pages=1, timeout=0.1)
    assert out["status"] == "unavailable"
    assert out["rows_fetched"] == 0
    assert out["error"]
