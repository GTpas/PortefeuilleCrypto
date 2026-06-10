"""
Offline tests for the portfolio advisor (pure logic, no network/DB).

Covers: posture mapping, allocation coherence (sum = 100, breadth rule),
per-asset safety caps (illiquid small caps, high volatility), action verbs,
weight distribution bounded by caps/budget, and the assembled advice block.
"""

from __future__ import annotations

import pytest

from reports import portfolio_advisor as pa


# ── posture ───────────────────────────────────────────────────────────────────

def test_posture_mapping():
    assert pa.posture("bullish", 0.7, 60, 0.1) == "offensif"
    assert pa.posture("bullish", 0.4, 60, 0.1) == "equilibre"   # breadth not confirming
    assert pa.posture("neutral", 0.5, 50, 0.0) == "equilibre"
    assert pa.posture("neutral", 0.2, 50, 0.0) == "defensif"    # negative breadth
    assert pa.posture("bearish", 0.4, 40, 0.0) == "defensif"
    assert pa.posture("bearish", 0.2, 15, 0.0) == "cash_majoritaire"  # capitulation
    # missing breadth/sentiment → neutral defaults, never a crash
    assert pa.posture("neutral", None, None, 0.0) == "equilibre"


def test_global_conviction():
    assert pa.global_conviction(80, "bullish", 0.8) == "forte"
    assert pa.global_conviction(60, "neutral", 0.5) == "moyenne"
    assert pa.global_conviction(40, "bullish", 0.9) == "faible"
    assert pa.global_conviction(None, "bullish", 0.9) == "faible"  # no data → humble


# ── allocations ───────────────────────────────────────────────────────────────

def test_allocations_sum_to_100_for_every_posture_and_profile():
    for posture in pa.POSTURES:
        models = pa.allocation_models(posture)
        for profile, prof in models.items():
            assert sum(prof["allocation"].values()) == 100, (posture, profile)


def test_defensive_postures_increase_cash_and_drop_small_caps():
    off = pa.allocation_models("offensif")
    cash = pa.allocation_models("cash_majoritaire")
    for profile in pa.PROFILES:
        assert cash[profile]["allocation"]["stables_cash"] > off[profile]["allocation"]["stables_cash"]
        assert cash[profile]["allocation"]["small_caps"] == 0


def test_negative_breadth_moves_mid_small_to_cash():
    base = pa.allocation_models("offensif")["agressif"]["allocation"]
    adj = pa.allocation_models("offensif", breadth_pct=0.3)["agressif"]["allocation"]
    assert adj["mid_caps"] == 0 and adj["small_caps"] == 0
    assert adj["stables_cash"] == base["stables_cash"] + base["mid_caps"] + base["small_caps"]
    assert sum(adj.values()) == 100


# ── per-asset safety caps ─────────────────────────────────────────────────────

def test_cap_tier_from_volume_percentile():
    assert pa.cap_tier("BTC", 0.99) == "btc_eth"
    assert pa.cap_tier("ETH", 0.10) == "btc_eth"
    assert pa.cap_tier("SOL", 0.90) == "large"
    assert pa.cap_tier("XYZ", 0.60) == "mid"
    assert pa.cap_tier("ABC", 0.10) == "small"
    assert pa.cap_tier("ABC", None) == "small"  # unknown → most conservative


def test_illiquid_small_cap_never_overweighted():
    # prudent: excluded entirely; others: hard cap at 1 %
    assert pa.max_weight_for("prudent", "small", liquidity_ratio=0.2, volatility_ratio=0.3) == 0.0
    assert pa.max_weight_for("equilibre", "small", 0.2, 0.3) <= pa.ILLIQUID_WEIGHT_CAP
    assert pa.max_weight_for("agressif", "small", 0.2, 0.3) <= pa.ILLIQUID_WEIGHT_CAP


def test_high_volatility_halves_the_cap():
    normal = pa.max_weight_for("agressif", "large", 0.8, 0.3)
    volatile = pa.max_weight_for("agressif", "large", 0.8, 0.9)
    assert volatile == pytest.approx(normal / 2)


def test_actions_mapping():
    assert pa.action_for("BUY", "forte", 80) == "renforcer"
    assert pa.action_for("BUY", "moyenne", 76) == "acheter"
    assert pa.action_for("SELL", "forte", 20) == "vendre"
    assert pa.action_for("SELL", "moyenne", 20) == "alléger"
    assert pa.action_for("AVOID", "forte", 10) == "éviter"
    assert pa.action_for("HOLD", "moyenne", 65) == "surveiller"
    assert pa.action_for("HOLD", "faible", 40) == "conserver"


# ── weights ───────────────────────────────────────────────────────────────────

def _buy(symbol, base, opp, liq=0.8, vol=0.3, vp=0.9):
    return {"symbol": symbol, "base": base, "opportunity_score": opp,
            "liquidity": liq, "volatility": vol, "volume_percentile": vp,
            "signal": "BUY", "confidence_score": 80}


def test_weights_respect_budget_and_caps():
    buys = [_buy("AAA/USDT", "AAA", 90), _buy("BBB/USDT", "BBB", 80),
            _buy("CCC/USDT", "CCC", 75, liq=0.3, vp=0.2)]  # illiquid small
    w = pa.recommended_weights(buys, "offensif", breadth_pct=0.7)
    models = pa.allocation_models("offensif", 0.7)
    for profile in pa.PROFILES:
        alloc = models[profile]["allocation"]
        budget = (alloc["large_caps"] + alloc["mid_caps"] + alloc["small_caps"]
                  + alloc["opportunistic"])
        total = sum(w[s][profile] for s in w)
        assert total <= budget + 0.01, profile
        for s in w:
            assert w[s][profile] <= pa.MAX_ASSET_WEIGHT[profile] + 0.01
    # the illiquid small cap is excluded for prudent, capped elsewhere
    assert w["CCC/USDT"]["prudent"] == 0.0
    assert w["CCC/USDT"]["agressif"] <= pa.ILLIQUID_WEIGHT_CAP


def test_btc_eth_use_core_bucket():
    buys = [_buy("BTC/USDT", "BTC", 85)]
    w = pa.recommended_weights(buys, "equilibre")
    alloc = pa.allocation_models("equilibre")["equilibre"]["allocation"]
    assert w["BTC/USDT"]["equilibre"] == pytest.approx(alloc["btc_eth"] / 2)


# ── assembled block ───────────────────────────────────────────────────────────

def test_build_portfolio_advice_block():
    assets = [_buy("AAA/USDT", "AAA", 85),
              {"symbol": "DDD/USDT", "base": "DDD", "signal": "HOLD",
               "opportunity_score": 55, "confidence_score": 70,
               "liquidity": 0.6, "volatility": 0.3, "volume_percentile": 0.5}]
    block = pa.build_portfolio_advice(assets, regime="bullish", breadth_pct=0.7, fear_greed=65)
    assert block["posture"] in pa.POSTURES
    assert block["global_conviction"] in ("forte", "moyenne", "faible")
    assert set(block["profiles"]) == set(pa.PROFILES)
    assert "AAA/USDT" in block["weights_by_symbol"]
    assert "DDD/USDT" not in block["weights_by_symbol"]  # only BUY gets a weight
    assert block["posture_justification"]
    assert "volume 24h" in block["cap_tier_note"]  # honest mcap-proxy disclosure
