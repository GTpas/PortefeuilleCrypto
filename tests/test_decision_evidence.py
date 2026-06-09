"""
Offline tests for the decision Source-Evidence assembler (pure, no DB).

Covers the invariants the cockpit relies on:
- factors grouped by category → market/risk/social evidence groups;
- contributions come straight from decision_factor.score_contribution;
- social availability driven by REAL evidence rows, never the 'social_unavailable'
  placeholder factor, never mock;
- honest stale/unavailable states; missing when nothing is present;
- never raises on missing/None fields.
"""

import pytest

from api.decision_evidence import (
    assemble_source_evidence, freshness_status,
    DEFAULT_AVAILABLE_MS, DEFAULT_STALE_MS,
)


def _market_factors():
    return [
        {"category": "market", "name": "ret_15m", "value": 0.0182, "contribution": 0.47,
         "explanation": "Positive 15m return supports BUY."},
        {"category": "market", "name": "spread_bps", "value": 2.308, "contribution": 0.9066,
         "explanation": "Tight spread."},
    ]


def _risk_factors():
    return [
        {"category": "risk", "name": "portfolio_vol", "value": 0.186, "contribution": 0.8757,
         "explanation": "Within risk bounds."},
        {"category": "risk", "name": "position_concentration", "value": 0.0, "contribution": 1.0,
         "explanation": "No concentration."},
    ]


def _audit(**over):
    base = {
        "has_sufficient_market": True, "has_sufficient_social": False,
        "market_data_age_ms": 340, "social_data_age_ms": None,
        "quality_grade": "partial", "degradation_reasons": ["social_data_unavailable"],
    }
    base.update(over)
    return base


# ── freshness_status ─────────────────────────────────────────

def test_freshness_thresholds():
    assert freshness_status(100, 5000, 60000) == "available"
    assert freshness_status(10000, 5000, 60000) == "stale"
    assert freshness_status(120000, 5000, 60000) == "unavailable"
    assert freshness_status(None, 5000, 60000) == "unavailable"
    assert freshness_status(-1, 5000, 60000) == "unavailable"


# ── assemble: market + risk present, social absent ──────────

def test_market_and_risk_present_social_unavailable():
    ev = assemble_source_evidence(
        decision_id=483, symbol="ETH/USDT", exchange_code="binance",
        snapshot={"quality_grade": "partial"},
        factors=_market_factors() + _risk_factors()
                + [{"category": "social", "name": "social_unavailable", "value": 0.0,
                    "contribution": 0.0, "explanation": "No real social feed configured"}],
        audit=_audit(), social_evidence=[],
    )
    groups = {g["type"]: g for g in ev["groups"]}
    assert groups["market"]["status"] == "available"
    assert groups["risk"]["status"] == "available"
    assert groups["social"]["status"] == "unavailable"
    # The placeholder social factor must NOT be surfaced as evidence.
    assert groups["social"]["metrics"] == []
    assert groups["social"]["reason"] == "social_data_unavailable"
    assert ev["status"] == "partial"
    assert any("Social evidence unavailable" in w for w in ev["warnings"])


def test_contributions_come_from_decision_factor():
    ev = assemble_source_evidence(
        decision_id=1, symbol="BTC/USDT", exchange_code="binance", snapshot={},
        factors=_market_factors(), audit=_audit(), social_evidence=[],
    )
    market = next(g for g in ev["groups"] if g["type"] == "market")
    by_name = {m["name"]: m for m in market["metrics"]}
    assert by_name["spread_bps"]["score_contribution"] == 0.9066
    assert by_name["ret_15m"]["value"] == 0.0182
    assert by_name["ret_15m"]["explanation"] == "Positive 15m return supports BUY."


# ── assemble: real social present ───────────────────────────

def test_real_social_evidence_makes_social_available():
    social = [{"source_name": "rss_news", "author_handle": "CoinDesk",
               "text": "ETH upgrade confirmed", "relevance_score": 0.9,
               "published_at": "2026-06-09T21:29:00Z"}]
    ev = assemble_source_evidence(
        decision_id=7, symbol="ETH/USDT", exchange_code="binance", snapshot={},
        factors=_market_factors() + _risk_factors(),
        audit=_audit(has_sufficient_social=True, social_data_age_ms=1200,
                     degradation_reasons=[]),
        social_evidence=social,
    )
    g = next(x for x in ev["groups"] if x["type"] == "social")
    assert g["status"] == "available"
    assert g["items"][0]["source_name"] == "rss_news"
    assert g["provider"] == "internal_social_engine"
    assert ev["status"] == "complete"


def test_social_items_empty_is_unavailable_even_if_flag_true_without_age():
    # has_sufficient_social True but no items and no age → still unavailable (no real rows)
    ev = assemble_source_evidence(
        decision_id=8, symbol="ETH/USDT", exchange_code="binance", snapshot={},
        factors=_market_factors(),
        audit=_audit(has_sufficient_social=True, social_data_age_ms=None),
        social_evidence=[],
    )
    g = next(x for x in ev["groups"] if x["type"] == "social")
    assert g["status"] == "unavailable"


# ── assemble: degraded inputs ───────────────────────────────

def test_audit_none_does_not_crash_and_marks_groups_stale():
    ev = assemble_source_evidence(
        decision_id=9, symbol="ETH/USDT", exchange_code="binance", snapshot={},
        factors=_market_factors(), audit=None, social_evidence=None,
    )
    market = next(g for g in ev["groups"] if g["type"] == "market")
    # Real factors exist but freshness unknown → stale, never 'unavailable'.
    assert market["status"] == "stale"
    assert ev["freshness"]["market_data_age_ms"] is None


def test_no_factors_no_social_is_missing():
    ev = assemble_source_evidence(
        decision_id=10, symbol="ETH/USDT", exchange_code="binance", snapshot={},
        factors=[], audit=None, social_evidence=[],
    )
    assert ev["status"] == "missing"
    assert all(g["status"] == "unavailable" for g in ev["groups"])


def test_market_stale_emits_warning():
    ev = assemble_source_evidence(
        decision_id=11, symbol="ETH/USDT", exchange_code="binance", snapshot={},
        factors=_market_factors(), audit=_audit(market_data_age_ms=30000),
        social_evidence=[],
    )
    assert next(g for g in ev["groups"] if g["type"] == "market")["status"] == "stale"
    assert any("stale" in w.lower() for w in ev["warnings"])


def test_missing_explanation_gets_neutral_text():
    ev = assemble_source_evidence(
        decision_id=12, symbol="X/USDT", exchange_code="binance", snapshot={},
        factors=[{"category": "market", "name": "depth_usd_10bps", "value": 5.0,
                  "contribution": 0.1, "explanation": None}],
        audit=_audit(), social_evidence=[],
    )
    m = next(g for g in ev["groups"] if g["type"] == "market")["metrics"][0]
    assert m["explanation"] == "Metric recorded in decision_factor without additional explanation."


def test_exchange_code_pinned_through():
    ev = assemble_source_evidence(
        decision_id=13, symbol="ETH/USDT", exchange_code="binance", snapshot={},
        factors=_market_factors(), audit=_audit(), social_evidence=[],
    )
    assert ev["exchange_code"] == "binance"
    assert next(g for g in ev["groups"] if g["type"] == "market")["exchange_code"] == "binance"
