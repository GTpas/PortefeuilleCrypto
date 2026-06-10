"""
Daily report — scoring formulas (PURE, no I/O)
==============================================

Single source of truth for every number in the Daily Crypto Intelligence Report.
All functions are pure (deterministic, no network/DB), so they are exhaustively
unit-testable offline and the worker + API can never disagree on a score.

Design principles
-----------------
* **Real data only.** Every input is a real Binance 24h-ticker field or a real
  macro value. Inputs the data source simply does not carry (1h / 7d / 30d
  change) are ``None`` → surfaced as ``N/A`` and they *lower* the confidence
  score. Nothing is fabricated.
* **Bounded & explainable.** Sub-ratios live in ``[0, 1]`` (or a clearly defined
  ratio for relative strength). The final scores are ``[0, 100]``. Every weight
  is a named module constant so the formula is auditable and centralized.
* **Prudent.** Predictions are probabilities clamped to a humble band
  (never 0 % / 100 %); the report layer always frames them as scenarios.

The only "model" here is a transparent, rules-based weighted blend. The code is
structured so a learned model could later replace ``opportunity_score`` /
``predict`` without touching the rest of the pipeline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# Tunable constants (centralized so the formula is auditable in one place)
# ──────────────────────────────────────────────────────────────────────────────

# Final Opportunity Score weights (sum = 1.0) + a risk penalty, per the
# decision-support spec: opportunity = 25% momentum + 20% trend + 15% volume
# + 15% relative strength + 10% liquidity + 10% market regime − 5% risk penalty.
OPP_W_MOMENTUM = 0.25
OPP_W_TREND_QUALITY = 0.20
OPP_W_VOLUME = 0.15
OPP_W_REL_STRENGTH = 0.15
OPP_W_LIQUIDITY = 0.10
OPP_W_MARKET_CTX = 0.10
OPP_W_RISK_PENALTY = 0.05       # subtracted, scaled by risk_score/100

# Risk Score weights (sum = 1.0). NOTE: the spec's "concentration_risk" needs
# holder/whale data we do not have (real-data-only rule) — its slot is taken by
# microstructure/spread risk, the closest real proxy of manipulability.
RISK_W_VOLATILITY = 0.30
RISK_W_DRAWDOWN = 0.25
RISK_W_ILLIQUIDITY = 0.20
RISK_W_DATA_QUALITY = 0.15      # missing fields + staleness
RISK_W_MICROSTRUCTURE = 0.10    # spread width (manipulability proxy)

# Signal thresholds (documented in docs/daily_crypto_report.md).
BUY_OPP_MIN = 75.0
BUY_RISK_MAX = 60.0
BUY_CONF_MIN = 65.0
BUY_MOMENTUM_MIN = 0.50         # momentum must be positive to BUY
BUY_LIQUIDITY_MIN = 0.35        # enough liquidity to enter/exit cleanly
AVOID_LIQUIDITY_MAX = 0.25      # liquidity_ratio below → too illiquid to trust
AVOID_SPREAD_BPS = 60.0         # spread wider than this → fragile / manipulable
AVOID_CONF_MIN = 35.0           # confidence below → not enough real data
AVOID_RISK_MIN = 80.0           # risk at/above → avoid regardless
PUMP_CHANGE_PCT = 18.0          # |24h move| beyond this needs volume support…
PUMP_SUPPORT_MAX = 0.35         # …otherwise it looks like a thin pump → AVOID
SELL_CHANGE_MAX = -3.0          # 24h change at/below (%) is part of a SELL
SELL_MOMENTUM_MAX = 0.40        # momentum_ratio below → downward
SELL_RISK_MIN = 55.0            # combined with negative momentum → SELL

# Normalization references (chosen so the dominant cap separates large caps from
# the long tail; only the relative ordering matters).
VOL_LOG_REF = 10.0              # 1e10 quote-vol → liquidity vol term ~1.0
TRADES_LOG_REF = 6.0            # 1e6 trades → trades term ~1.0
CHANGE_MOMENTUM_SCALE = 8.0     # ±8 % 24h saturates the momentum tanh
RANGE_VOL_REF = 0.20            # 20 % intraday H-L range → volatility term 1.0
DRAWDOWN_REF = 0.15            # 15 % below the day high → drawdown term 1.0
SPREAD_LIQ_REF = 50.0           # ≥50 bps spread → liquidity spread term 0
SPREAD_RISK_REF = 40.0          # ≥40 bps spread → spread risk term 1.0
VWAP_GAP_REF = 0.03             # ±3 % from VWAP saturates the alignment term
REL_STRENGTH_REF = 0.10         # +10 % vs BTC → rel-strength term 1.0

# Prediction is intentionally humble: never below/above this band.
UP_PROB_FLOOR = 0.15
UP_PROB_CEIL = 0.85


# ──────────────────────────────────────────────────────────────────────────────
# Small pure helpers
# ──────────────────────────────────────────────────────────────────────────────

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _finite(x) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _norm_log(x: Optional[float], ref_log10: float) -> float:
    """log10(x)/ref, clamped to [0,1]. 0 for non-positive/None."""
    v = _finite(x)
    if v is None or v <= 1:
        return 0.0
    return clamp(math.log10(v) / ref_log10)


def range_position(last: Optional[float], low: Optional[float],
                   high: Optional[float]) -> Optional[float]:
    """Where ``last`` sits in the 24h [low, high] range → [0,1]. None if invalid."""
    last, low, high = _finite(last), _finite(low), _finite(high)
    if last is None or low is None or high is None or high <= low:
        return None
    return clamp((last - low) / (high - low))


# ──────────────────────────────────────────────────────────────────────────────
# Inputs (normalized once by the generator, then scored purely)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AssetInput:
    """One asset's real, normalized inputs for the daily report.

    Every field is either a real Binance 24h-ticker value or ``None`` when the
    source genuinely does not provide it. ``volume_percentile`` is the asset's
    rank of 24h quote volume within the universe (filled by the generator) — a
    real cross-sectional measure of relative activity/conviction.
    """
    symbol: str
    base: str = ""
    name: Optional[str] = None
    price: Optional[float] = None
    open_24h: Optional[float] = None
    high_24h: Optional[float] = None
    low_24h: Optional[float] = None
    vwap_24h: Optional[float] = None
    change_24h: Optional[float] = None        # percent
    quote_volume: Optional[float] = None      # 24h quote-notional
    base_volume: Optional[float] = None
    num_trades: Optional[int] = None
    spread_bps: Optional[float] = None
    volume_percentile: Optional[float] = None  # [0,1] within the universe
    stale: bool = False


@dataclass
class MarketContext:
    """Whole-market backdrop shared by every asset (built from the universe +
    global-context macro tier). Scalars only, so per-asset scoring stays pure."""
    btc_change_24h: Optional[float] = None     # 24h % of BTC/USDT
    breadth_pct: Optional[float] = None        # share of universe with change>0 [0,1]
    fear_greed: Optional[float] = None         # [0,100]
    mcap_change_24h: Optional[float] = None     # global market-cap 24h %
    regime: str = "neutral"                     # bullish | neutral | bearish


# ──────────────────────────────────────────────────────────────────────────────
# Ratios (each real, bounded, documented)
# ──────────────────────────────────────────────────────────────────────────────

def momentum_ratio(a: AssetInput) -> Optional[float]:
    """Is the asset accelerating up (→1) or down (→0)? 24h-only (1h/7d/30d N/A).

    Blends the normalized 24h change with where price sits in the day's range
    (both real). Returns None only if even the 24h change is missing.
    """
    chg = _finite(a.change_24h)
    if chg is None:
        return None
    core = 0.5 + 0.5 * math.tanh(chg / CHANGE_MOMENTUM_SCALE)
    rpos = range_position(a.price, a.low_24h, a.high_24h)
    if rpos is None:
        return clamp(core)
    return clamp(0.65 * core + 0.35 * rpos)


def _vwap_alignment(a: AssetInput) -> Optional[float]:
    """Price vs 24h VWAP → [0,1]. >0.5 means trading above its volume-weighted
    average (accumulation), <0.5 below (distribution). Real volume-based signal."""
    last, vwap = _finite(a.price), _finite(a.vwap_24h)
    if last is None or vwap is None or vwap <= 0:
        return None
    gap = (last - vwap) / vwap
    return clamp(0.5 + 0.5 * (gap / VWAP_GAP_REF))


def volume_confirmation_ratio(a: AssetInput) -> Optional[float]:
    """Is the move backed by activity? Blends relative activity (volume
    percentile) with VWAP alignment (price above its VWAP = buyers in control).

    NOTE (limitation, documented): a true volume-vs-its-own-average ratio needs
    historical volume the universe tier doesn't keep, so we proxy conviction with
    the cross-sectional volume percentile. Honest and real, but coarser than a
    per-symbol baseline. Returns None if neither input is available.
    """
    vp = _finite(a.volume_percentile)
    align = _vwap_alignment(a)
    if vp is None and align is None:
        return None
    if align is None:
        return clamp(vp)
    if vp is None:
        return clamp(align)
    return clamp(0.6 * vp + 0.4 * align)


def liquidity_ratio(a: AssetInput) -> float:
    """How easily tradable: 24h quote volume + trade count + spread tightness."""
    vol_term = _norm_log(a.quote_volume, VOL_LOG_REF)
    trades_term = _norm_log(a.num_trades, TRADES_LOG_REF)
    sp = _finite(a.spread_bps)
    spread_term = (1.0 - clamp(sp / SPREAD_LIQ_REF)) if sp is not None else 0.5
    return clamp(0.55 * vol_term + 0.20 * trades_term + 0.25 * spread_term)


def volatility_ratio(a: AssetInput) -> float:
    """Realized 24h volatility proxy: intraday range + absolute 24h move."""
    last, high, low = _finite(a.price), _finite(a.high_24h), _finite(a.low_24h)
    rng = 0.0
    if last and high is not None and low is not None and last > 0 and high >= low:
        rng = clamp((high - low) / last / RANGE_VOL_REF)
    chg = _finite(a.change_24h)
    chg_term = clamp(abs(chg) / 20.0) if chg is not None else 0.0
    return clamp(0.6 * rng + 0.4 * chg_term)


def drawdown_from_high(a: AssetInput) -> float:
    """Intraday drawdown: how far ``last`` is below the 24h high → [0,1] risk."""
    last, high = _finite(a.price), _finite(a.high_24h)
    if last is None or high is None or high <= 0:
        return 0.0
    return clamp((high - last) / high / DRAWDOWN_REF)


def relative_strength_btc(a: AssetInput, ctx: MarketContext) -> Optional[float]:
    """24h relative strength vs BTC, centered at 1.0. None if either move missing.

    >1 means the asset outperformed BTC over 24h; <1 underperformed.
    """
    chg = _finite(a.change_24h)
    btc = _finite(ctx.btc_change_24h)
    if chg is None or btc is None:
        return None
    denom = 1.0 + btc / 100.0
    if denom <= 0:
        return None
    return (1.0 + chg / 100.0) / denom


def _rel_strength_norm(rs: Optional[float]) -> float:
    """Map the rel-strength ratio to [0,1] (1.0 → 0.5; +REL_STRENGTH_REF → 1.0)."""
    if rs is None:
        return 0.5
    return clamp(0.5 + 0.5 * ((rs - 1.0) / REL_STRENGTH_REF))


def trend_quality_ratio(a: AssetInput) -> Optional[float]:
    """Quality/coherence of the move: penalize a big price move on thin volume
    (classic low-liquidity pump). High when the move is supported by volume+VWAP.
    """
    support = volume_confirmation_ratio(a)
    chg = _finite(a.change_24h)
    if support is None and chg is None:
        return None
    support = 0.5 if support is None else support
    move_mag = clamp(abs(chg) / 15.0) if chg is not None else 0.0
    # Unsupported magnitude (move bigger than the conviction behind it) hurts.
    unsupported = max(0.0, move_mag - support)
    return clamp(0.5 * support + 0.5 * (1.0 - unsupported))


def market_context_score(ctx: MarketContext) -> float:
    """Macro backdrop in [0,1] (same for all assets): Fear&Greed + mcap 24h +
    market breadth. Missing inputs default to neutral 0.5."""
    fg = _finite(ctx.fear_greed)
    fg_term = clamp(fg / 100.0) if fg is not None else 0.5
    mc = _finite(ctx.mcap_change_24h)
    mc_term = clamp(0.5 + 0.5 * (mc / 5.0)) if mc is not None else 0.5
    br = _finite(ctx.breadth_pct)
    br_term = clamp(br) if br is not None else 0.5
    return clamp(0.4 * fg_term + 0.3 * mc_term + 0.3 * br_term)


# ──────────────────────────────────────────────────────────────────────────────
# Data completeness / confidence
# ──────────────────────────────────────────────────────────────────────────────

# Real fields we expect from the Binance 24h ticker. Horizons 1h/7d/30d are NOT
# here — they are knowingly unavailable and handled as N/A (with a confidence cap).
_CORE_FIELDS = ("price", "change_24h", "quote_volume", "num_trades",
                "high_24h", "low_24h", "vwap_24h", "spread_bps")
# Only intraday (24h) horizon is available → confidence is capped to stay humble.
HORIZON_COVERAGE = 0.7


def completeness(a: AssetInput) -> float:
    """Share of expected real fields actually present → [0,1]."""
    present = sum(1 for f in _CORE_FIELDS if _finite(getattr(a, f)) is not None)
    return present / len(_CORE_FIELDS)


def missing_features(a: AssetInput) -> list[str]:
    """Which inputs are unavailable (for honest N/A reporting)."""
    miss = [f for f in _CORE_FIELDS if _finite(getattr(a, f)) is None]
    # Horizons the data source never provides — always reported as N/A.
    miss += ["change_1h", "change_7d", "change_30d", "market_cap", "depth_l2"]
    return miss


def confidence_score(a: AssetInput) -> float:
    """How much to trust this asset's signal → [0,100].

    Driven by data completeness, liquidity (liquid markets = more reliable
    reads), freshness (stale → penalized), and a horizon cap (only 24h available,
    so we never claim full confidence).
    """
    comp = completeness(a)
    liq = liquidity_ratio(a)
    fresh = 0.0 if a.stale else 1.0
    raw = 0.35 * comp + 0.30 * liq + 0.20 * fresh + 0.15 * HORIZON_COVERAGE
    return round(100.0 * clamp(raw), 1)


# ──────────────────────────────────────────────────────────────────────────────
# Composite scores
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AssetScores:
    opportunity_score: float
    risk_score: float
    confidence_score: float
    momentum_ratio: Optional[float]
    volume_confirmation_ratio: Optional[float]
    liquidity_ratio: float
    relative_strength_btc: Optional[float]
    trend_quality_ratio: Optional[float]
    volatility_ratio: float
    drawdown_ratio: float
    market_context_score: float
    components: dict = field(default_factory=dict)       # opportunity sub-weights
    risk_components: dict = field(default_factory=dict)  # risk sub-weights


def opportunity_score(a: AssetInput, ctx: MarketContext) -> AssetScores:
    """Compute every ratio + the final opportunity/risk/confidence scores.

    Risk is computed first because the opportunity formula subtracts a risk
    penalty (OPP_W_RISK_PENALTY · risk/100), per the decision-support spec.
    """
    mom = momentum_ratio(a)
    volc = volume_confirmation_ratio(a)
    liq = liquidity_ratio(a)
    rs = relative_strength_btc(a, ctx)
    rs_norm = _rel_strength_norm(rs)
    tq = trend_quality_ratio(a)
    vol = volatility_ratio(a)
    dd = drawdown_from_high(a)
    mkt = market_context_score(ctx)
    conf = confidence_score(a)

    data_quality_risk = clamp(0.7 * (1.0 - completeness(a)) + (0.3 if a.stale else 0.0))
    risk_components = {
        "volatility": round(RISK_W_VOLATILITY * vol, 4),
        "drawdown": round(RISK_W_DRAWDOWN * dd, 4),
        "illiquidity": round(RISK_W_ILLIQUIDITY * (1.0 - liq), 4),
        "data_quality": round(RISK_W_DATA_QUALITY * data_quality_risk, 4),
        "microstructure": round(RISK_W_MICROSTRUCTURE * _spread_risk(a), 4),
    }
    risk = 100.0 * clamp(sum(risk_components.values()))

    # Missing ratio → neutral 0.5 in the blend (never a fabricated extreme).
    c_mom = mom if mom is not None else 0.5
    c_volc = volc if volc is not None else 0.5
    c_tq = tq if tq is not None else 0.5

    components = {
        "momentum": round(OPP_W_MOMENTUM * c_mom, 4),
        "trend_quality": round(OPP_W_TREND_QUALITY * c_tq, 4),
        "volume_confirmation": round(OPP_W_VOLUME * c_volc, 4),
        "relative_strength": round(OPP_W_REL_STRENGTH * rs_norm, 4),
        "liquidity": round(OPP_W_LIQUIDITY * liq, 4),
        "market_context": round(OPP_W_MARKET_CTX * mkt, 4),
        "risk_penalty": round(-OPP_W_RISK_PENALTY * (risk / 100.0), 4),
    }
    opp = 100.0 * clamp(sum(components.values()))

    return AssetScores(
        opportunity_score=round(opp, 1),
        risk_score=round(risk, 1),
        confidence_score=conf,
        momentum_ratio=_r(mom), volume_confirmation_ratio=_r(volc),
        liquidity_ratio=round(liq, 4), relative_strength_btc=_r(rs, 4),
        trend_quality_ratio=_r(tq), volatility_ratio=round(vol, 4),
        drawdown_ratio=round(dd, 4), market_context_score=round(mkt, 4),
        components=components, risk_components=risk_components,
    )


def sub_scores(s: AssetScores) -> dict:
    """Every sub-score on a 0–100 scale (None preserved when the underlying
    ratio is genuinely unavailable). Single source for the report's per-asset
    score block — the UI never recomputes numbers."""
    def pct(x: Optional[float]) -> Optional[float]:
        return None if x is None else round(100.0 * clamp(x), 1)
    return {
        "momentum_score": pct(s.momentum_ratio),
        "trend_score": pct(s.trend_quality_ratio),
        "volume_score": pct(s.volume_confirmation_ratio),
        "liquidity_score": pct(s.liquidity_ratio),
        "volatility_score": pct(s.volatility_ratio),
        "drawdown_score": pct(s.drawdown_ratio),
        "relative_strength_score": pct(_rel_strength_norm(s.relative_strength_btc)),
        "market_regime_score": pct(s.market_context_score),
        "risk_score": round(s.risk_score, 1),
        "opportunity_score": round(s.opportunity_score, 1),
        "confidence_score": round(s.confidence_score, 1),
    }


def _spread_risk(a: AssetInput) -> float:
    sp = _finite(a.spread_bps)
    if sp is None:
        return 0.5  # unknown spread = moderate risk, never 0
    return clamp(sp / SPREAD_RISK_REF)


def _r(x: Optional[float], dp: int = 4) -> Optional[float]:
    return None if x is None else round(x, dp)


# ──────────────────────────────────────────────────────────────────────────────
# Rating scale (A+ → E)
# ──────────────────────────────────────────────────────────────────────────────

# Public, documented scale. Each band: definition + simple explanation + numeric
# rule. Exposed via the API/markdown so the cockpit can render the legend.
RATING_SCALE = [
    {"rating": "A+", "label": "Opportunité très forte",
     "definition": "Opportunité très forte, risque contrôlé, données solides.",
     "simple": "Configuration parmi les meilleures du jour, avec un risque maîtrisé."},
    {"rating": "A", "label": "Opportunité forte",
     "definition": "Opportunité forte, bon momentum, liquidité correcte.",
     "simple": "Bon profil global, à surveiller pour une entrée."},
    {"rating": "B", "label": "Intéressante mais prudence",
     "definition": "Opportunité intéressante mais prudence nécessaire.",
     "simple": "Du potentiel, mais le signal n'est pas encore franc."},
    {"rating": "C", "label": "Neutre / incertain",
     "definition": "Signal neutre ou incertain.",
     "simple": "Rien de clair : mieux vaut attendre une confirmation."},
    {"rating": "D", "label": "Risque élevé",
     "definition": "Risque élevé ou tendance dégradée.",
     "simple": "Profil dégradé : la prudence s'impose."},
    {"rating": "E", "label": "À éviter",
     "definition": "À éviter : données faibles ou risque extrême.",
     "simple": "Trop risqué ou trop peu de données fiables pour se positionner."},
]


def rating(opp: float, risk: float, confidence: float) -> str:
    """Map opportunity/risk/confidence to an A+→E rating (deterministic).

    Uses a composite (opportunity minus a fraction of risk), gated by confidence:
    a low-confidence asset can never earn a top rating regardless of opportunity.
    """
    composite = opp - 0.4 * risk
    if confidence < AVOID_CONF_MIN:
        # Too little real data to trust → cap at the bottom of the scale.
        return "E" if composite < 18 else "D"
    if composite >= 70 and risk <= 40 and confidence >= 70:
        return "A+"
    if composite >= 58 and risk <= 50 and confidence >= 60:
        return "A"
    if composite >= 45:
        return "B"
    if composite >= 32:
        return "C"
    if composite >= 18:
        return "D"
    return "E"


# ──────────────────────────────────────────────────────────────────────────────
# Signal (BUY / HOLD / SELL / AVOID)
# ──────────────────────────────────────────────────────────────────────────────

def signal(a: AssetInput, s: AssetScores) -> str:
    """Decide the final signal. Order matters: AVOID gates first (data/liquidity
    quality), then BUY, then SELL, else HOLD."""
    liq = s.liquidity_ratio
    sp = _finite(a.spread_bps)
    chg = _finite(a.change_24h)
    mom = s.momentum_ratio if s.momentum_ratio is not None else 0.5
    volc = s.volume_confirmation_ratio if s.volume_confirmation_ratio is not None else 0.5
    # AVOID: not enough real data / too illiquid / too risky to act on.
    if (a.stale or liq < AVOID_LIQUIDITY_MAX or s.confidence_score < AVOID_CONF_MIN
            or s.risk_score >= AVOID_RISK_MIN or (sp is not None and sp > AVOID_SPREAD_BPS)):
        return "AVOID"
    # AVOID (pump suspect): extreme 24h move without volume/VWAP support.
    if chg is not None and abs(chg) >= PUMP_CHANGE_PCT and volc < PUMP_SUPPORT_MAX:
        return "AVOID"
    # BUY: strong opportunity, contained risk, enough confidence, positive
    # momentum and sufficient liquidity (decision-support spec).
    if (s.opportunity_score >= BUY_OPP_MIN and s.risk_score <= BUY_RISK_MAX
            and s.confidence_score >= BUY_CONF_MIN and mom > BUY_MOMENTUM_MIN
            and liq >= BUY_LIQUIDITY_MIN):
        return "BUY"
    # SELL: clear downward momentum + (elevated risk or short-term trend break,
    # i.e. price below its 24h VWAP — distribution).
    below_vwap = (a.price is not None and a.vwap_24h is not None
                  and _finite(a.vwap_24h) and a.price < a.vwap_24h)
    if (chg is not None and chg <= SELL_CHANGE_MAX and mom < SELL_MOMENTUM_MAX
            and (s.risk_score >= SELL_RISK_MIN or (below_vwap and s.drawdown_ratio >= 0.5))):
        return "SELL"
    return "HOLD"


def conviction(sig: str, s: AssetScores) -> str:
    """Conviction level of the signal: forte | moyenne | faible.

    Driven by confidence (data quality) and by how far the scores sit from the
    decision thresholds — a BUY barely above the bar is a weak BUY."""
    if s.confidence_score < 50:
        return "faible"
    if sig == "BUY":
        margin = (s.opportunity_score - BUY_OPP_MIN) + (BUY_RISK_MAX - s.risk_score)
        if margin >= 25 and s.confidence_score >= 75:
            return "forte"
        return "moyenne" if margin >= 8 else "faible"
    if sig == "SELL":
        mom = s.momentum_ratio if s.momentum_ratio is not None else 0.5
        if s.risk_score >= 70 and mom <= 0.25 and s.confidence_score >= 65:
            return "forte"
        return "moyenne"
    if sig == "AVOID":
        return "forte" if (s.confidence_score < AVOID_CONF_MIN or s.risk_score >= AVOID_RISK_MIN) else "moyenne"
    # HOLD: conviction is about how clearly neutral the situation is.
    if 40 <= s.opportunity_score <= 65 and s.confidence_score >= 70:
        return "moyenne"
    return "faible"


def contradictions(a: AssetInput, s: AssetScores) -> list[str]:
    """Real contradictory signals worth disclosing next to a recommendation."""
    out: list[str] = []
    mom = s.momentum_ratio
    volc = s.volume_confirmation_ratio
    if mom is not None and volc is not None:
        if mom >= 0.6 and volc < 0.45:
            out.append("momentum positif mais volume peu confirmant")
        if mom <= 0.4 and volc >= 0.6:
            out.append("momentum négatif mais volume acheteur soutenu")
    if mom is not None and mom >= 0.6 and s.risk_score >= 60:
        out.append("tendance positive mais risque élevé")
    rs = s.relative_strength_btc
    if rs is not None and mom is not None:
        if mom >= 0.6 and rs < 0.97:
            out.append("hausse 24h mais sous-performance vs BTC")
    if s.market_context_score < 0.4 and mom is not None and mom >= 0.6:
        out.append("actif haussier dans un marché global défavorable")
    if s.volatility_ratio >= 0.6 and s.liquidity_ratio < 0.5:
        out.append("forte volatilité sur une liquidité moyenne/faible")
    return out


def horizon(a: AssetInput, s: AssetScores) -> str:
    """Suggested holding horizon label (heuristic, from volatility/liquidity)."""
    if s.volatility_ratio >= 0.6:
        return "intraday"
    if s.liquidity_ratio >= 0.6 and s.volatility_ratio < 0.35:
        return "moyen terme"
    return "swing"


# ──────────────────────────────────────────────────────────────────────────────
# Prediction (transparent, prudent, rules-based)
# ──────────────────────────────────────────────────────────────────────────────

def up_probability(a: AssetInput, ctx: MarketContext, s: AssetScores) -> float:
    """Indicative probability of a near-term up move, clamped to a humble band.

    A linear, transparent blend of directional drivers (momentum, relative
    strength, volume confirmation, macro) minus a risk penalty — NOT a claim of
    certainty. Always within [UP_PROB_FLOOR, UP_PROB_CEIL].
    """
    mom = s.momentum_ratio if s.momentum_ratio is not None else 0.5
    rs = _rel_strength_norm(s.relative_strength_btc)
    volc = s.volume_confirmation_ratio if s.volume_confirmation_ratio is not None else 0.5
    mkt = s.market_context_score
    raw = (1.6 * (mom - 0.5) + 0.8 * (rs - 0.5) + 0.6 * (volc - 0.5)
           + 0.8 * (mkt - 0.5) - 0.6 * (s.risk_score / 100.0 - 0.5))
    return round(clamp(0.5 + raw, UP_PROB_FLOOR, UP_PROB_CEIL), 3)


def direction_label(up_prob: float) -> str:
    if up_prob >= 0.62:
        return "haussier"
    if up_prob >= 0.55:
        return "neutre_a_haussier"
    if up_prob > 0.45:
        return "neutre"
    if up_prob > 0.38:
        return "neutre_a_baissier"
    return "baissier"


def confidence_label(confidence: float) -> str:
    if confidence >= 70:
        return "élevé"
    if confidence >= 50:
        return "modéré"
    return "faible"
