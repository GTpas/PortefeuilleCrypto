"""
Daily report — portfolio advisor (PURE, no I/O)
===============================================

Translates the per-asset signals of the daily report into portfolio-level
guidance: a recommended market posture, model allocations for three investor
profiles (prudent / équilibré / agressif), and a per-asset action with
recommended weights.

Design principles (same as ``reports.scoring``):
* **Pure & testable.** Deterministic functions of already-scored assets +
  market context. No network, no DB.
* **Real data only.** Cap tiers are derived from the *real* cross-sectional
  24h-volume percentile (a liquidity-tier proxy — true market cap is not
  available without an aggregator and is never fabricated). The proxy is
  labeled as such everywhere it surfaces.
* **Safety rules are code, not prose** — illiquid small caps are never
  overweighted, very volatile tokens are capped, defensive regimes shift the
  budget to BTC/ETH + stables/cash. Each rule is a named function.

This layer produces *model* allocations ("portefeuille type"), not personalized
advice; the report carries the corresponding framing.
"""

from __future__ import annotations

from typing import Optional

from reports import scoring

PROFILES = ("prudent", "equilibre", "agressif")
PROFILE_LABELS_FR = {"prudent": "Prudent", "equilibre": "Équilibré", "agressif": "Agressif"}

POSTURES = ("offensif", "equilibre", "defensif", "cash_majoritaire")
POSTURE_LABELS_FR = {
    "offensif": "Offensif", "equilibre": "Équilibré",
    "defensif": "Défensif", "cash_majoritaire": "Cash majoritaire",
}

# Allocation buckets (sum = 100 per profile). Keys are stable identifiers the
# frontend renders; values are percentages of the model portfolio.
BUCKETS = ("btc_eth", "large_caps", "mid_caps", "small_caps", "stables_cash", "opportunistic")
BUCKET_LABELS_FR = {
    "btc_eth": "BTC / ETH", "large_caps": "Large caps", "mid_caps": "Mid caps",
    "small_caps": "Small caps", "stables_cash": "Stablecoins / cash",
    "opportunistic": "Opportunités court terme",
}

# Model allocations per posture × profile (percent, sums to 100). Defensive
# postures deliberately concentrate on BTC/ETH + stables (spec rule).
_ALLOCATIONS = {
    "offensif": {
        "prudent":   {"btc_eth": 40, "large_caps": 15, "mid_caps": 5,  "small_caps": 0,  "stables_cash": 35, "opportunistic": 5},
        "equilibre": {"btc_eth": 40, "large_caps": 20, "mid_caps": 10, "small_caps": 5,  "stables_cash": 15, "opportunistic": 10},
        "agressif":  {"btc_eth": 30, "large_caps": 20, "mid_caps": 15, "small_caps": 10, "stables_cash": 10, "opportunistic": 15},
    },
    "equilibre": {
        "prudent":   {"btc_eth": 35, "large_caps": 10, "mid_caps": 0,  "small_caps": 0,  "stables_cash": 50, "opportunistic": 5},
        "equilibre": {"btc_eth": 40, "large_caps": 15, "mid_caps": 5,  "small_caps": 0,  "stables_cash": 35, "opportunistic": 5},
        "agressif":  {"btc_eth": 35, "large_caps": 20, "mid_caps": 10, "small_caps": 5,  "stables_cash": 20, "opportunistic": 10},
    },
    "defensif": {
        "prudent":   {"btc_eth": 25, "large_caps": 5,  "mid_caps": 0,  "small_caps": 0,  "stables_cash": 70, "opportunistic": 0},
        "equilibre": {"btc_eth": 30, "large_caps": 10, "mid_caps": 0,  "small_caps": 0,  "stables_cash": 55, "opportunistic": 5},
        "agressif":  {"btc_eth": 35, "large_caps": 15, "mid_caps": 5,  "small_caps": 0,  "stables_cash": 40, "opportunistic": 5},
    },
    "cash_majoritaire": {
        "prudent":   {"btc_eth": 15, "large_caps": 0,  "mid_caps": 0,  "small_caps": 0,  "stables_cash": 85, "opportunistic": 0},
        "equilibre": {"btc_eth": 20, "large_caps": 5,  "mid_caps": 0,  "small_caps": 0,  "stables_cash": 75, "opportunistic": 0},
        "agressif":  {"btc_eth": 25, "large_caps": 10, "mid_caps": 0,  "small_caps": 0,  "stables_cash": 60, "opportunistic": 5},
    },
}

# Risk framing per profile (estimates from historical crypto drawdowns, clearly
# labeled as heuristics — never a promise).
_PROFILE_RISK = {
    "prudent":   {"risk_level": "faible à modéré", "expected_drawdown": "≈ 10–20 % (estimation)", "horizon": "long terme (12 mois et plus)"},
    "equilibre": {"risk_level": "modéré",          "expected_drawdown": "≈ 20–35 % (estimation)", "horizon": "moyen / long terme (6–18 mois)"},
    "agressif":  {"risk_level": "élevé",            "expected_drawdown": "≈ 35–60 % (estimation)", "horizon": "moyen terme, gestion active (3–12 mois)"},
}

# Per-asset weight caps (percent of the model portfolio) — spec safety rules.
MAX_ASSET_WEIGHT = {"prudent": 5.0, "equilibre": 8.0, "agressif": 12.0}
SMALL_CAP_MAX = {"prudent": 0.0, "equilibre": 2.0, "agressif": 4.0}
ILLIQUID_LIQUIDITY_MAX = 0.45   # below → illiquid: hard-capped / excluded
ILLIQUID_WEIGHT_CAP = 1.0       # an illiquid asset never exceeds 1 %
HIGH_VOLATILITY_RATIO = 0.70    # above → cap halved (spec: limit very volatile tokens)

CORE_BASES = ("BTC", "ETH")

# Volume-percentile boundaries of the liquidity-tier proxy (real cross-sectional
# measure; market cap is unavailable → never fabricated).
TIER_LARGE_MIN = 0.85
TIER_MID_MIN = 0.55


# ──────────────────────────────────────────────────────────────────────────────
# Market posture & global conviction
# ──────────────────────────────────────────────────────────────────────────────

def posture(regime: str, breadth_pct: Optional[float], fear_greed: Optional[float],
            buy_ratio: float) -> str:
    """Recommended positioning from the market backdrop.

    offensif        — bullish regime confirmed by breadth.
    equilibre       — neutral / mixed conditions.
    defensif        — bearish regime or deteriorating breadth.
    cash_majoritaire— bearish + capitulation-grade sentiment/breadth.
    """
    br = breadth_pct if breadth_pct is not None else 0.5
    fg = fear_greed if fear_greed is not None else 50.0
    if regime == "bearish":
        if fg <= 25 or br <= 0.25:
            return "cash_majoritaire"
        return "defensif"
    if regime == "bullish":
        if br >= 0.55 and buy_ratio > 0.0:
            return "offensif"
        return "equilibre"
    # neutral regime: lean defensive when breadth is clearly negative.
    if br <= 0.35:
        return "defensif"
    return "equilibre"


def global_conviction(avg_confidence: Optional[float], regime: str,
                      breadth_pct: Optional[float]) -> str:
    """forte | moyenne | faible — how much to trust today's overall read."""
    conf = avg_confidence if avg_confidence is not None else 0.0
    clarity = 0.0
    if breadth_pct is not None:
        clarity = abs(breadth_pct - 0.5) * 2.0  # 0 = mixed, 1 = one-sided market
    if conf >= 70 and (regime != "neutral" or clarity >= 0.4):
        return "forte"
    if conf >= 55:
        return "moyenne"
    return "faible"


def posture_justification(p: str, regime: str, breadth_pct: Optional[float],
                          fear_greed: Optional[float]) -> str:
    br = f"{round(breadth_pct * 100)} % des cryptos suivies en hausse 24h" if breadth_pct is not None else "largeur de marché indisponible"
    fg = f"Fear & Greed à {round(fear_greed)}" if fear_greed is not None else "sentiment indisponible"
    regime_fr = {"bullish": "haussier", "neutral": "neutre", "bearish": "baissier"}.get(regime, "neutre")
    if p == "offensif":
        return (f"Régime {regime_fr} confirmé par la largeur de marché ({br}, {fg}) : "
                f"le modèle privilégie une exposition crypto élevée, tout en conservant un socle BTC/ETH.")
    if p == "defensif":
        return (f"Régime {regime_fr} ({br}, {fg}) : le modèle réduit l'exposition aux altcoins, "
                f"concentre sur BTC/ETH et augmente la poche stablecoins/cash.")
    if p == "cash_majoritaire":
        return (f"Régime {regime_fr} avec sentiment dégradé ({br}, {fg}) : le modèle privilégie la "
                f"préservation du capital — poche cash/stablecoins majoritaire, exposition résiduelle BTC/ETH.")
    return (f"Conditions mixtes ({br}, {fg}) : le modèle recommande une exposition équilibrée, "
            f"socle BTC/ETH + sélection resserrée d'altcoins liquides, sans levier directionnel fort.")


# ──────────────────────────────────────────────────────────────────────────────
# Allocation models
# ──────────────────────────────────────────────────────────────────────────────

def _adjust_for_breadth(alloc: dict, breadth_pct: Optional[float]) -> dict:
    """Spec rule: mid/small caps only when breadth is positive — otherwise their
    budget moves to stables/cash."""
    if breadth_pct is None or breadth_pct >= 0.5:
        return dict(alloc)
    out = dict(alloc)
    moved = out.get("mid_caps", 0) + out.get("small_caps", 0)
    out["mid_caps"] = 0
    out["small_caps"] = 0
    out["stables_cash"] = out.get("stables_cash", 0) + moved
    return out


def allocation_models(p: str, breadth_pct: Optional[float] = None) -> dict:
    """Model allocation per profile for the given posture (percent, sum 100)."""
    base = _ALLOCATIONS.get(p, _ALLOCATIONS["equilibre"])
    out = {}
    for profile in PROFILES:
        alloc = _adjust_for_breadth(base[profile], breadth_pct)
        out[profile] = {
            "label": PROFILE_LABELS_FR[profile],
            "allocation": alloc,
            **_PROFILE_RISK[profile],
        }
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Per-asset tiering, action & weights
# ──────────────────────────────────────────────────────────────────────────────

def cap_tier(base: str, volume_percentile: Optional[float]) -> str:
    """Liquidity-tier proxy (real 24h-volume percentile; NOT true market cap)."""
    if (base or "").upper() in CORE_BASES:
        return "btc_eth"
    vp = volume_percentile
    if vp is None:
        return "small"  # unknown activity → most conservative tier
    if vp >= TIER_LARGE_MIN:
        return "large"
    if vp >= TIER_MID_MIN:
        return "mid"
    return "small"


def action_for(sig: str, conv: str, opportunity: float) -> str:
    """Map signal + conviction to a portfolio action verb."""
    if sig == "BUY":
        return "renforcer" if conv == "forte" else "acheter"
    if sig == "SELL":
        return "vendre" if conv == "forte" else "alléger"
    if sig == "AVOID":
        return "éviter"
    return "surveiller" if opportunity >= 60 else "conserver"


def max_weight_for(profile: str, tier: str, liquidity_ratio: float,
                   volatility_ratio: float) -> float:
    """Per-asset weight ceiling (percent) after every safety rule.

    Rules (spec): illiquid assets hard-capped (excluded for the prudent
    profile); small caps tightly capped per profile; very volatile tokens
    capped at half; never above the profile's absolute max."""
    cap = MAX_ASSET_WEIGHT[profile]
    if tier == "small":
        cap = min(cap, SMALL_CAP_MAX[profile])
    if liquidity_ratio < ILLIQUID_LIQUIDITY_MAX:
        if profile == "prudent":
            return 0.0
        cap = min(cap, ILLIQUID_WEIGHT_CAP)
    if volatility_ratio >= HIGH_VOLATILITY_RATIO:
        cap = cap / 2.0
    return round(cap, 2)


def recommended_weights(buy_assets: list[dict], p: str,
                        breadth_pct: Optional[float] = None) -> dict:
    """Distribute each profile's non-core crypto budget across the BUY list.

    ``buy_assets``: scored asset dicts (signal == BUY) carrying at least
    ``symbol``, ``base``, ``opportunity_score``, ``liquidity``, ``volatility``
    and ``volume_percentile``. Returns {symbol: {profile: weight_pct}}.
    Weights are proportional to opportunity score, then clipped by
    ``max_weight_for`` — the sum can therefore be below budget (the remainder
    stays in stables/cash, which is intended, never forced into bad assets)."""
    models = allocation_models(p, breadth_pct)
    out: dict[str, dict[str, float]] = {a["symbol"]: {} for a in buy_assets}
    for profile in PROFILES:
        alloc = models[profile]["allocation"]
        budget = (alloc.get("large_caps", 0) + alloc.get("mid_caps", 0)
                  + alloc.get("small_caps", 0) + alloc.get("opportunistic", 0))
        candidates = []
        for a in buy_assets:
            tier = cap_tier(a.get("base", ""), a.get("volume_percentile"))
            if tier == "btc_eth":
                # BTC/ETH live in their own bucket, not the altcoin budget.
                out[a["symbol"]][profile] = round(alloc.get("btc_eth", 0) / 2.0, 2)
                continue
            cap = max_weight_for(profile, tier, a.get("liquidity") or 0.0,
                                 a.get("volatility") or 0.0)
            if cap <= 0:
                out[a["symbol"]][profile] = 0.0
                continue
            candidates.append((a, cap))
        total_opp = sum(max(a.get("opportunity_score") or 0.0, 1.0) for a, _ in candidates)
        for a, cap in candidates:
            share = max(a.get("opportunity_score") or 0.0, 1.0) / total_opp if total_opp else 0.0
            out[a["symbol"]][profile] = round(min(budget * share, cap), 2)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Top-level advice block
# ──────────────────────────────────────────────────────────────────────────────

def build_portfolio_advice(assets: list[dict], *, regime: str,
                           breadth_pct: Optional[float],
                           fear_greed: Optional[float]) -> dict:
    """Assemble the report's ``portfolio_models`` block from scored assets."""
    n = max(len(assets), 1)
    buys = [a for a in assets if a.get("signal") == "BUY"]
    buy_ratio = len(buys) / n
    p = posture(regime, breadth_pct, fear_greed, buy_ratio)

    top = sorted(assets, key=lambda a: a.get("opportunity_score") or 0.0, reverse=True)[:20]
    confs = [a.get("confidence_score") for a in top if a.get("confidence_score") is not None]
    avg_conf = (sum(confs) / len(confs)) if confs else None
    conviction = global_conviction(avg_conf, regime, breadth_pct)

    weights = recommended_weights(buys, p, breadth_pct)
    profiles = allocation_models(p, breadth_pct)
    for profile in PROFILES:
        profiles[profile]["justification"] = _profile_justification(profile, p)

    return {
        "posture": p,
        "posture_label": POSTURE_LABELS_FR[p],
        "posture_justification": posture_justification(p, regime, breadth_pct, fear_greed),
        "global_conviction": conviction,
        "average_confidence_top20": round(avg_conf, 1) if avg_conf is not None else None,
        "buy_ratio": round(buy_ratio, 4),
        "profiles": profiles,
        "bucket_labels": BUCKET_LABELS_FR,
        "weights_by_symbol": weights,
        "cap_tier_note": ("Les tiers large/mid/small sont dérivés du percentile de volume 24h réel "
                          "(proxy de taille par liquidité) — la capitalisation exacte n'est pas "
                          "disponible et n'est jamais fabriquée."),
    }


def _profile_justification(profile: str, p: str) -> str:
    base = {
        "prudent": "Priorité à la préservation du capital : socle BTC/ETH, poche cash importante, pas de small caps.",
        "equilibre": "Compromis rendement/risque : socle BTC/ETH majoritaire, sélection d'altcoins liquides, réserve de cash.",
        "agressif": "Recherche de performance : exposition altcoins élargie, mais chaque position reste plafonnée par les règles de liquidité/volatilité.",
    }[profile]
    if p in ("defensif", "cash_majoritaire"):
        return base + " Le régime de marché actuel réduit l'exposition globale recommandée."
    return base
