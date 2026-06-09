"""
Decision source-evidence assembler
-----------------------------------
Builds the structured ``source_evidence`` block surfaced in the cockpit's
"Decision Drill-down" modal, from data **already persisted** for the decision —
it never recomputes a parallel decision and never fabricates a source.

Invariants (project-wide, enforced here):
- Real data only. Absence of data → an explicit ``unavailable`` / ``stale``
  status, never a fabricated value or provider.
- Mock social content is filtered upstream (the caller's SQL applies
  ``COALESCE(ts.name,'') NOT ILIKE 'mock%'``); this module assumes the social
  evidence it receives is already real-only.
- ``assemble_source_evidence`` is **pure** (no I/O): the SQL lives in the API
  route, the formatting/grouping lives here, so it is fully unit-tested offline.

Factor → evidence mapping: factors persisted in ``decision_factor`` are grouped
by ``factor_category`` (market / risk / social). The metric names/values/
contributions/explanations shown are exactly those persisted — same names the
drill-down bar chart plots — so every plotted factor is traceable.
"""

from __future__ import annotations

from typing import Any, Optional

# Freshness thresholds (ms). Overridable from config; these are the code-level
# defaults so the module is import-safe and testable without settings.
DEFAULT_AVAILABLE_MS = 5000
DEFAULT_STALE_MS = 60000

_GROUP_META = {
    "market": {
        "label": "Market Evidence",
        "provider": "internal_market_features",
        "source_table": "market_feature_1s",
    },
    "risk": {
        "label": "Risk Evidence",
        "provider": "internal_risk_engine",
        "source_table": "decision_factor / portfolio_state",
    },
    "social": {
        "label": "Social Evidence",
        "provider": "internal_social_engine",
        "source_table": "decision_evidence_link / raw_content",
    },
}

_NEUTRAL_EXPLANATION = "Metric recorded in decision_factor without additional explanation."


def freshness_status(age_ms: Optional[float], available_ms: int, stale_ms: int) -> str:
    """available (fresh) | stale (old but usable) | unavailable (null/too old)."""
    if age_ms is None:
        return "unavailable"
    try:
        age = float(age_ms)
    except (TypeError, ValueError):
        return "unavailable"
    if age < 0:
        return "unavailable"
    if age < available_ms:
        return "available"
    if age < stale_ms:
        return "stale"
    return "unavailable"


def _group_status(has_content: bool, age_ms: Optional[float], available_ms: int, stale_ms: int) -> str:
    """
    Status of an evidence group. Persisted factors ARE real, traceable evidence,
    so a group that HAS content is never 'unavailable' — at worst 'stale' when
    freshness can't be confirmed (null/old age). 'unavailable' is reserved for
    groups with no content at all.
    """
    if not has_content:
        return "unavailable"
    fs = freshness_status(age_ms, available_ms, stale_ms)
    return fs if fs in ("available", "stale") else "stale"


def _metric_from_factor(f: dict) -> dict:
    explanation = f.get("explanation")
    return {
        "name": f.get("name"),
        "value": f.get("value"),
        "score_contribution": f.get("contribution"),
        "explanation": explanation if explanation else _NEUTRAL_EXPLANATION,
    }


def assemble_source_evidence(
    *,
    decision_id: int,
    symbol: str,
    exchange_code: Optional[str],
    snapshot: dict,
    factors: list[dict],
    audit: Optional[dict],
    social_evidence: Optional[list[dict]] = None,
    available_ms: int = DEFAULT_AVAILABLE_MS,
    stale_ms: int = DEFAULT_STALE_MS,
    generated_at: Optional[str] = None,
) -> dict:
    """
    Build the ``source_evidence`` dict from already-fetched decision data.

    PURE — no DB/network. ``factors`` are dicts with keys
    {category,name,value,contribution,explanation} (as returned by the decision
    route). ``audit`` may be None. ``social_evidence`` is the real-only,
    mock-filtered list of evidence rows (may be empty/None).
    """
    factors = factors or []
    social_evidence = social_evidence or []
    audit = audit or {}

    market_age = audit.get("market_data_age_ms")
    social_age = audit.get("social_data_age_ms")
    has_sufficient_market = bool(audit.get("has_sufficient_market"))
    has_sufficient_social = bool(audit.get("has_sufficient_social"))
    degradation_reasons = list(audit.get("degradation_reasons") or [])
    quality_grade = audit.get("quality_grade") or snapshot.get("quality_grade") or "unknown"

    by_cat: dict[str, list[dict]] = {"market": [], "risk": [], "social": []}
    for f in factors:
        cat = (f.get("category") or "").lower()
        if cat in by_cat:
            by_cat[cat].append(f)

    groups: list[dict] = []
    warnings: list[str] = []

    # ── Market group ──────────────────────────
    market_metrics = [_metric_from_factor(f) for f in by_cat["market"]]
    market_status = _group_status(bool(market_metrics), market_age, available_ms, stale_ms)
    groups.append({
        "type": "market",
        "label": _GROUP_META["market"]["label"],
        "status": market_status,
        "provider": _GROUP_META["market"]["provider"] if market_metrics else None,
        "exchange_code": exchange_code,
        "source_table": _GROUP_META["market"]["source_table"],
        "age_ms": market_age if market_metrics else None,
        "metrics": market_metrics,
        "items": [],
    })
    if market_status == "stale" and market_age is not None:
        warnings.append(f"Market data is stale. Last update: {_fmt_age(market_age)} ago.")
    elif not market_metrics:
        warnings.append("Market evidence unavailable for this decision.")

    # ── Risk group ────────────────────────────
    # Risk factors are deterministic from the same evaluation snapshot, so we
    # carry the market data age as the freshness proxy (there is no separate
    # risk-data clock). No metrics → unavailable.
    risk_metrics = [_metric_from_factor(f) for f in by_cat["risk"]]
    risk_status = _group_status(bool(risk_metrics), market_age, available_ms, stale_ms)
    groups.append({
        "type": "risk",
        "label": _GROUP_META["risk"]["label"],
        "status": risk_status,
        "provider": _GROUP_META["risk"]["provider"] if risk_metrics else None,
        "source_table": _GROUP_META["risk"]["source_table"],
        "age_ms": market_age if risk_metrics else None,
        "metrics": risk_metrics,
        "items": [],
    })

    # ── Social group ──────────────────────────
    # Availability is driven by REAL evidence rows (decision_evidence_link, already
    # mock-filtered upstream), NOT by social factors — the 'social_unavailable'
    # placeholder factor must never be presented as evidence.
    social_items = [_social_item(e) for e in social_evidence]
    social_available = bool(social_items) or (has_sufficient_social and social_age is not None)
    social_metrics = [_metric_from_factor(f) for f in by_cat["social"]] if social_available else []
    social_status = _group_status(social_available, social_age, available_ms, stale_ms)
    social_reason = None
    if social_status == "unavailable":
        social_reason = degradation_reasons[0] if degradation_reasons else "social_data_unavailable"
        warnings.append("Social evidence unavailable. Decision computed from market and risk factors only.")
    groups.append({
        "type": "social",
        "label": _GROUP_META["social"]["label"],
        "status": social_status,
        "provider": _GROUP_META["social"]["provider"] if social_available else None,
        "source_table": _GROUP_META["social"]["source_table"],
        "age_ms": social_age if social_available else None,
        "reason": social_reason,
        "metrics": social_metrics,
        "items": social_items,
    })

    # ── Overall status ────────────────────────
    any_market = market_status in ("available", "stale")
    any_risk = risk_status in ("available", "stale")
    any_social = social_status in ("available", "stale")
    if any_market and any_risk and any_social:
        status = "complete"
    elif not any_market and not any_risk and not any_social:
        status = "missing"
    else:
        status = "partial"

    return {
        "status": status,
        "decision_id": decision_id,
        "symbol": symbol,
        "exchange_code": exchange_code,
        "generated_at": generated_at,
        "quality": {
            "quality_grade": quality_grade,
            "has_sufficient_market": has_sufficient_market,
            "has_sufficient_social": has_sufficient_social,
            "degradation_reasons": degradation_reasons,
        },
        "freshness": {
            "market_data_age_ms": market_age,
            "social_data_age_ms": social_age,
            "status": freshness_status(market_age, available_ms, stale_ms),
        },
        "groups": groups,
        "warnings": warnings,
    }


def _social_item(e: dict) -> dict:
    return {
        "source_name": e.get("source_name"),
        "author_handle": e.get("author_handle"),
        "text": e.get("text"),
        "relevance_score": e.get("relevance_score"),
        "published_at": e.get("published_at"),
        "source_url": e.get("source_url"),
    }


def _fmt_age(age_ms: Optional[float]) -> str:
    if age_ms is None:
        return "n/a"
    try:
        secs = float(age_ms) / 1000.0
    except (TypeError, ValueError):
        return "n/a"
    if secs < 1:
        return f"{int(age_ms)}ms"
    return f"{secs:.0f}s"
