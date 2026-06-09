"""
Offline tests for the Daily Crypto Intelligence Report (no network, no DB).

Covers the parts that decide what the report says and how it is built:
  * pure ratios are bounded + directionally correct (real-data-only);
  * rating bands + BUY/HOLD/SELL/AVOID signal logic at documented thresholds;
  * missing-data robustness (no crash, honest N/A, lower confidence);
  * predictions are prudent (probability strictly inside a humble band);
  * full report assembly (JSON shape) + Markdown render + 300-symbol performance;
  * store save/load/history round-trip;
  * the worker's pure scheduling helpers.
"""

from __future__ import annotations

import time

import pytest

from reports import scoring, generator
from reports.scoring import AssetInput, MarketContext


# ── helpers ───────────────────────────────────────────────────────────────────

def mk_row(symbol, *, price=100.0, open=99.0, high=105.0, low=95.0, vwap=99.5,
           change=2.0, qvol=5_000_000.0, bvol=None, trades=50_000, spread=5.0,
           stale=False):
    base = symbol.split("/")[0]
    return {
        "symbol": symbol, "base": base, "price": price, "open": open,
        "high": high, "low": low, "weighted_avg_price": vwap, "change_pct": change,
        "quote_volume": qvol, "base_volume": bvol if bvol is not None else (qvol / price if price else 0),
        "num_trades": trades, "spread_bps": spread, "stale": stale, "source": "binance_spot",
    }


def score_row(row, ctx=None):
    a = generator._row_to_input(row)
    generator._fill_volume_percentiles([a])  # single-asset → 0.5
    ctx = ctx or MarketContext(btc_change_24h=0.5, breadth_pct=0.6, fear_greed=60, mcap_change_24h=1.0)
    s = scoring.opportunity_score(a, ctx)
    return a, s


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_clamp():
    assert scoring.clamp(1.5) == 1.0
    assert scoring.clamp(-0.5) == 0.0
    assert scoring.clamp(0.3) == 0.3


def test_range_position():
    assert scoring.range_position(100, 90, 110) == pytest.approx(0.5)
    assert scoring.range_position(110, 90, 110) == 1.0
    assert scoring.range_position(90, 90, 110) == 0.0
    assert scoring.range_position(100, None, 110) is None
    assert scoring.range_position(100, 110, 110) is None  # degenerate range


def test_momentum_direction_and_bounds():
    up = AssetInput("X/USDT", change_24h=10.0, price=109, low_24h=100, high_24h=110)
    down = AssetInput("Y/USDT", change_24h=-10.0, price=101, low_24h=100, high_24h=110)
    mu, md = scoring.momentum_ratio(up), scoring.momentum_ratio(down)
    assert 0.0 <= md < 0.5 < mu <= 1.0
    # missing change → None (never fabricated)
    assert scoring.momentum_ratio(AssetInput("Z/USDT")) is None


def test_liquidity_monotonic_in_volume():
    low = AssetInput("A/USDT", quote_volume=1e5, num_trades=100, spread_bps=20)
    high = AssetInput("B/USDT", quote_volume=5e9, num_trades=2e6, spread_bps=2)
    lo, hi = scoring.liquidity_ratio(low), scoring.liquidity_ratio(high)
    assert 0.0 <= lo < hi <= 1.0


def test_relative_strength_vs_btc():
    ctx = MarketContext(btc_change_24h=2.0)
    out = AssetInput("OUT/USDT", change_24h=10.0)   # beats BTC
    under = AssetInput("UND/USDT", change_24h=-5.0)  # lags BTC
    assert scoring.relative_strength_btc(out, ctx) > 1.0
    assert scoring.relative_strength_btc(under, ctx) < 1.0
    # missing BTC → None (no fabrication)
    assert scoring.relative_strength_btc(out, MarketContext()) is None


def test_volume_confirmation_uses_vwap():
    # Same volume percentile, price above vs below VWAP → above is stronger.
    above = AssetInput("U/USDT", price=102, vwap_24h=100, change_24h=3, volume_percentile=0.5)
    below = AssetInput("D/USDT", price=98, vwap_24h=100, change_24h=3, volume_percentile=0.5)
    assert scoring.volume_confirmation_ratio(above) > scoring.volume_confirmation_ratio(below)


def test_confidence_penalizes_missing_and_stale():
    full = AssetInput("F/USDT", price=100, open_24h=99, high_24h=101, low_24h=98,
                      vwap_24h=100, change_24h=1, quote_volume=5e9, num_trades=1e6, spread_bps=2)
    sparse = AssetInput("S/USDT", price=100, change_24h=1)
    stale = AssetInput("T/USDT", price=100, open_24h=99, high_24h=101, low_24h=98,
                       vwap_24h=100, change_24h=1, quote_volume=5e9, num_trades=1e6,
                       spread_bps=2, stale=True)
    assert scoring.confidence_score(full) > scoring.confidence_score(sparse)
    assert scoring.confidence_score(full) > scoring.confidence_score(stale)
    # Horizon cap keeps us humble even with perfect data.
    assert scoring.confidence_score(full) <= 100.0


# ── rating bands ──────────────────────────────────────────────────────────────

def test_rating_bands():
    assert scoring.rating(86, 11, 94) == "A+"
    assert scoring.rating(70, 15, 65) == "A"   # composite 64 ≥ 58, risk ≤ 50, conf ≥ 60
    assert scoring.rating(50, 12, 60) == "B"
    assert scoring.rating(40, 12, 60) == "C"
    assert scoring.rating(24, 12, 60) == "D"
    assert scoring.rating(10, 70, 60) == "E"
    # low confidence caps the rating regardless of opportunity
    assert scoring.rating(90, 5, 30) in ("D", "E")


# ── signals ───────────────────────────────────────────────────────────────────

def test_signal_buy():
    row = mk_row("ALT/USDT", price=104.5, open=99, high=105, low=98, vwap=101,
                 change=5.0, qvol=5e9, trades=2_000_000, spread=2.0)
    a, s = score_row(row, MarketContext(btc_change_24h=0.5, breadth_pct=0.7, fear_greed=70, mcap_change_24h=3))
    a.volume_percentile = 0.95  # liquid leader
    s = scoring.opportunity_score(a, MarketContext(btc_change_24h=0.5, breadth_pct=0.7, fear_greed=70, mcap_change_24h=3))
    assert s.opportunity_score >= scoring.BUY_OPP_MIN
    assert s.risk_score <= scoring.BUY_RISK_MAX
    assert scoring.signal(a, s) == "BUY"


def test_signal_sell():
    row = mk_row("WEAK/USDT", price=89, open=100, high=105, low=88, vwap=97,
                 change=-12.0, qvol=2e6, trades=20_000, spread=45.0)
    a, s = score_row(row)
    assert s.risk_score >= scoring.SELL_RISK_MIN
    assert scoring.signal(a, s) == "SELL"


def test_signal_avoid_when_stale_or_wide_spread():
    a1, s1 = score_row(mk_row("STALE/USDT", stale=True))
    assert scoring.signal(a1, s1) == "AVOID"
    a2, s2 = score_row(mk_row("WIDE/USDT", spread=70.0))
    assert scoring.signal(a2, s2) == "AVOID"


def test_signal_hold_default():
    a, s = score_row(mk_row("MID/USDT", change=1.0, qvol=8e7, spread=8.0))
    assert scoring.signal(a, s) == "HOLD"


# ── missing-data robustness ───────────────────────────────────────────────────

def test_all_missing_does_not_crash():
    a = AssetInput("EMPTY/USDT")
    ctx = MarketContext()
    s = scoring.opportunity_score(a, ctx)
    assert 0 <= s.opportunity_score <= 100
    assert 0 <= s.risk_score <= 100
    sig = scoring.signal(a, s)
    assert sig in ("BUY", "HOLD", "SELL", "AVOID")
    # horizons + market cap are always reported missing (never fabricated)
    miss = scoring.missing_features(a)
    assert "change_7d" in miss and "market_cap" in miss


# ── prediction prudence ───────────────────────────────────────────────────────

def test_prediction_is_bounded_and_never_certain():
    for change in (-30, -5, 0, 5, 30):
        row = mk_row("P/USDT", change=change)
        a, s = score_row(row)
        up = scoring.up_probability(a, MarketContext(btc_change_24h=0), s)
        assert scoring.UP_PROB_FLOOR <= up <= scoring.UP_PROB_CEIL
        assert up not in (0.0, 1.0)  # never a certainty


# ── full report assembly ──────────────────────────────────────────────────────

def _universe(n):
    rows = [mk_row("BTC/USDT", change=1.0, qvol=2e10, trades=3e6, price=68000,
                   open=67000, high=69000, low=66000, vwap=67500)]
    for i in range(n - 1):
        rows.append(mk_row(f"C{i}/USDT", change=(i % 11) - 5, qvol=1e6 * (i + 1),
                           trades=1000 * (i + 1), price=10 + i,
                           high=11 + i, low=9 + i, vwap=10 + i, spread=3 + (i % 20)))
    return rows


def test_build_report_shape():
    rep = generator.build_daily_report(_universe(20), {"sentiment": {"real": True, "value": 55},
                                       "market": {"real": True, "market_cap_change_24h_pct": 1.2}},
                                       generated_at="2026-06-10T00:00:00+00:00",
                                       report_date="2026-06-10")
    assert rep["universe_size"] == 20
    assert rep["market_regime"] in ("bullish", "neutral", "bearish")
    assert set(rep["signal_counts"]) == {"BUY", "HOLD", "SELL", "AVOID"}
    assert len(rep["assets"]) == 20
    a0 = rep["assets"][0]
    # ranking by opportunity desc
    assert a0["rank"] == 1
    assert rep["assets"][0]["opportunity_score"] >= rep["assets"][-1]["opportunity_score"]
    # required per-asset keys
    for k in ("symbol", "signal", "rating", "opportunity_score", "risk_score",
              "confidence_score", "prediction", "metrics", "explanation_simple",
              "source_evidence", "horizon", "justification"):
        assert k in a0
    # unavailable horizons honestly None
    assert a0["change_7d"] is None and a0["market_cap"] is None
    assert rep["disclaimer"]
    assert "conseil financier" in rep["disclaimer"]


def test_source_evidence_is_honest_not_fabricated():
    rep = generator.build_daily_report(_universe(5), None,
                                       generated_at="2026-06-10T00:00:00+00:00",
                                       report_date="2026-06-10")
    ev = rep["assets"][0]["source_evidence"]
    # real price evidence present + available
    price_ev = [e for e in ev if e["metric"] == "price"][0]
    assert price_ev["available"] is True and price_ev["value"] is not None
    # unavailable horizons explicitly marked unavailable, value None
    unav = [e for e in ev if e["source"] == "unavailable"]
    assert unav and all(e["available"] is False and e["value"] is None for e in unav)


def test_markdown_render_contains_sections():
    rep = generator.build_daily_report(_universe(8), None,
                                       generated_at="2026-06-10T00:00:00+00:00",
                                       report_date="2026-06-10")
    md = generator.render_markdown(rep)
    assert "# Rapport Crypto Quotidien — 2026-06-10" in md
    assert "Résumé exécutif" in md
    assert "Distribution des ratings" in md
    assert "Classement global" in md
    assert "conseil financier" in md  # disclaimer present


def test_build_report_300_performance():
    rows = _universe(300)
    t0 = time.time()
    rep = generator.build_daily_report(rows, None,
                                       generated_at="2026-06-10T00:00:00+00:00",
                                       report_date="2026-06-10")
    elapsed = time.time() - t0
    assert rep["universe_size"] == 300
    assert len(rep["assets"]) == 300
    # Must be fast (well under a second on CI); generous bound to avoid flakiness.
    assert elapsed < 3.0


# ── store round-trip ──────────────────────────────────────────────────────────

def test_store_roundtrip_and_history(tmp_path):
    from reports.store import ReportStore
    store = ReportStore(str(tmp_path))
    rep = generator.build_daily_report(_universe(6), None,
                                       generated_at="2026-06-10T00:00:00+00:00",
                                       report_date="2026-06-10")
    md = generator.render_markdown(rep)
    entry = store.save(rep, md)
    assert entry["report_date"] == "2026-06-10"

    loaded = store.load("2026-06-10")
    assert loaded["universe_size"] == 6
    assert store.latest_date() == "2026-06-10"
    assert store.load_markdown("2026-06-10").startswith("# Rapport")

    # second date → history newest-first
    rep2 = dict(rep); rep2["report_date"] = "2026-06-11"
    store.save(rep2, generator.render_markdown(rep2))
    hist = store.history(10)
    assert [h["report_date"] for h in hist] == ["2026-06-11", "2026-06-10"]
    assert store.load("2026-13-99") is None  # invalid date → honest None


# ── worker pure helpers ───────────────────────────────────────────────────────

def test_next_run_at_rolls_to_next_day():
    from datetime import datetime, timezone
    from workers.report_worker import next_run_at, resolve_tz
    tz = resolve_tz("UTC")
    now = datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)
    nxt = next_run_at(now, 0, 0, tz)        # 00:00 already passed today
    assert nxt.year == 2026 and nxt.month == 6 and nxt.day == 11 and nxt.hour == 0
    nxt2 = next_run_at(now, 23, 30, tz)     # later today
    assert nxt2.day == 10 and nxt2.hour == 23 and nxt2.minute == 30


def test_resolve_tz_falls_back_to_utc():
    from datetime import timezone
    from workers.report_worker import resolve_tz
    assert resolve_tz("UTC") == timezone.utc
    assert resolve_tz("Not/AZone") == timezone.utc  # invalid → UTC, never raises
