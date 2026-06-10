"""
Daily report — generator (JSON report + French Markdown render)
===============================================================

Assembles the Daily Crypto Intelligence Report from data passed in (the live
universe rows + the macro global-context snapshot). Pure in spirit: data in →
report out, no network/DB here (the worker/API fetch the data, ``store`` persists
the result). All numbers come from ``reports.scoring`` (the single source of
truth), so the worker and the API produce identical reports from the same inputs.

Real data only: unavailable inputs (1h/7d/30d change, market cap, L2 depth) are
emitted as ``None`` with an explicit reason, never fabricated. Predictions are
framed as probabilities/scenarios and the report carries a not-financial-advice
disclaimer.
"""

from __future__ import annotations

from typing import Optional

from reports import portfolio_advisor, scoring
from reports.scoring import AssetInput, MarketContext, AssetScores

REPORT_KIND = "Rapport d'aide à la décision financière — recommandations générées par modèle quantitatif"

DISCLAIMER = (
    "Rapport d'aide à la décision financière — recommandations générées par un modèle "
    "quantitatif à partir de données de marché réelles (Binance Spot 24h, CoinGecko, "
    "DefiLlama, alternative.me). Les crypto-actifs sont volatils : les recommandations "
    "dépendent de la qualité et de la disponibilité des données au moment de la génération, "
    "et les scénarios sont exprimés en probabilités, jamais en certitudes."
)

# Slim per-asset fields for the executive-summary top-lists.
_SLIM = ("rank", "symbol", "name", "signal", "rating", "conviction", "action",
         "opportunity_score", "risk_score", "confidence_score", "price",
         "change_24h", "justification", "explanation_simple")


def _f(x) -> Optional[float]:
    return scoring._finite(x)


# ──────────────────────────────────────────────────────────────────────────────
# Input normalization
# ──────────────────────────────────────────────────────────────────────────────

def _row_to_input(row: dict) -> AssetInput:
    """One universe row (light Binance ticker) → a normalized AssetInput."""
    base = row.get("base") or (row.get("symbol", "").split("/")[0])
    return AssetInput(
        symbol=row.get("symbol", "?"),
        base=base,
        name=row.get("name") or base or None,
        price=_f(row.get("price")),
        open_24h=_f(row.get("open")),
        high_24h=_f(row.get("high")),
        low_24h=_f(row.get("low")),
        vwap_24h=_f(row.get("weighted_avg_price")),
        change_24h=_f(row.get("change_pct")),
        quote_volume=_f(row.get("quote_volume")),
        base_volume=_f(row.get("base_volume")),
        num_trades=int(_f(row.get("num_trades")) or 0) or None,
        spread_bps=_f(row.get("spread_bps")),
        stale=bool(row.get("stale")),
    )


def _fill_volume_percentiles(inputs: list[AssetInput]) -> None:
    """Cross-sectional 24h-quote-volume percentile per asset (real relative
    activity). Mutates ``volume_percentile`` in place."""
    vols = sorted(a.quote_volume for a in inputs if a.quote_volume is not None)
    n = len(vols)
    if n <= 1:
        for a in inputs:
            a.volume_percentile = 0.5 if a.quote_volume is not None else None
        return
    import bisect
    for a in inputs:
        if a.quote_volume is None:
            a.volume_percentile = None
            continue
        # share of the universe with a strictly-lower volume → [0,1]
        a.volume_percentile = bisect.bisect_left(vols, a.quote_volume) / (n - 1)


def _build_context(inputs: list[AssetInput], global_context: Optional[dict],
                   btc_symbol: str) -> MarketContext:
    btc = next((a for a in inputs if a.symbol == btc_symbol), None)
    btc_change = btc.change_24h if btc else None

    changes = [a.change_24h for a in inputs if a.change_24h is not None]
    breadth = (sum(1 for c in changes if c > 0) / len(changes)) if changes else None

    fg = mcap_chg = None
    gc = global_context or {}
    sent = gc.get("sentiment") or {}
    if sent.get("real"):
        fg = _f(sent.get("value"))
    mkt = gc.get("market") or {}
    if mkt.get("real"):
        mcap_chg = _f(mkt.get("market_cap_change_24h_pct"))

    ctx = MarketContext(btc_change_24h=btc_change, breadth_pct=breadth,
                        fear_greed=fg, mcap_change_24h=mcap_chg)
    ctx.regime = classify_regime(ctx)
    return ctx


def classify_regime(ctx: MarketContext) -> str:
    """bullish | neutral | bearish from breadth + Fear&Greed + mcap 24h change."""
    score = 0.0
    n = 0
    if ctx.breadth_pct is not None:
        score += (ctx.breadth_pct - 0.5) * 2.0      # [-1,1]
        n += 1
    if ctx.fear_greed is not None:
        score += (ctx.fear_greed - 50.0) / 50.0     # [-1,1]
        n += 1
    if ctx.mcap_change_24h is not None:
        score += scoring.clamp(ctx.mcap_change_24h / 4.0, -1.0, 1.0)
        n += 1
    if n == 0:
        return "neutral"
    avg = score / n
    if avg >= 0.15:
        return "bullish"
    if avg <= -0.15:
        return "bearish"
    return "neutral"


# ──────────────────────────────────────────────────────────────────────────────
# Per-asset assembly
# ──────────────────────────────────────────────────────────────────────────────

_DIRECTION_FR = {
    "haussier": "haussier", "neutre_a_haussier": "neutre à haussier",
    "neutre": "neutre", "neutre_a_baissier": "neutre à baissier",
    "baissier": "baissier",
}


def _prediction(a: AssetInput, ctx: MarketContext, s: AssetScores) -> dict:
    up = scoring.up_probability(a, ctx, s)
    down = round(1.0 - up, 3)
    direction = scoring.direction_label(up)
    conf_lbl = scoring.confidence_label(s.confidence_score)

    high = a.high_24h
    low = a.low_24h
    lvl_hi = f"{high:g}" if high is not None else "le plus haut récent"
    lvl_lo = f"{low:g}" if low is not None else "le plus bas récent"

    central = (f"Scénario central ({_DIRECTION_FR[direction]}) : probabilité de hausse "
               f"estimée à {round(up*100)} %. Mouvement attendu dans la zone "
               f"{lvl_lo}–{lvl_hi} tant qu'aucun catalyseur ne change la donne.")
    bullish = (f"Scénario haussier : un dépassement franc de {lvl_hi} confirmé par le "
               f"volume ouvrirait la voie à une poursuite de la hausse.")
    bearish = (f"Scénario baissier : une cassure sous {lvl_lo} avec volume vendeur "
               f"invaliderait le support et augmenterait le risque de baisse.")
    invalidation = (f"Scénario invalidé si le prix casse durablement sous {lvl_lo} "
                    f"(perte du support 24h) ou si le volume s'effondre sur un mouvement.")

    return {
        "direction": direction,
        "up_probability": up,
        "down_probability": down,
        "confidence_level": conf_lbl,
        "scenario": central,
        "bullish_case": bullish,
        "bearish_case": bearish,
        "invalidation": invalidation,
        # Honest horizon coverage: only 24h is real from the Binance ticker.
        "horizons": {
            "24h": {"available": True, "basis": "Binance 24h ticker (réel)"},
            "7d": {"available": False, "reason": "historique 7 jours non disponible (N/A)"},
            "30d": {"available": False, "reason": "historique 30 jours non disponible (N/A)"},
        },
    }


def _freshness_label(age_ms: Optional[float]) -> str:
    if age_ms is None:
        return "âge inconnu"
    if age_ms < 5_000:
        return "temps réel (<5 s)"
    if age_ms < 60_000:
        return f"frais ({round(age_ms/1000)} s)"
    return f"périmé ({round(age_ms/1000)} s)"


def _source_evidence(a: AssetInput, ctx: MarketContext, row: dict) -> list[dict]:
    """Real, traceable evidence: every input used by the model, its source, its
    timestamp and its freshness. Only fields that actually exist — an absent
    input is listed as 'Donnée indisponible' with the reason, never fabricated."""
    src = row.get("source") or "binance_spot"
    as_of_ms = row.get("updated_at")
    age_ms = row.get("age_ms")
    fresh = _freshness_label(age_ms)
    ev: list[dict] = []

    def add(source, metric, value, available, note="", *, as_of=None, age=None):
        ev.append({"source": source, "metric": metric, "value": value,
                   "available": available, "note": note,
                   "as_of_ms": as_of, "age_ms": age})

    base_note = f"ticker 24h Binance Spot (WS !ticker@arr + REST), {fresh}"
    add(src, "price", a.price, a.price is not None, base_note, as_of=as_of_ms, age=age_ms)
    add(src, "change_24h_pct", a.change_24h, a.change_24h is not None,
        "variation 24h glissantes (réelle)", as_of=as_of_ms, age=age_ms)
    add(src, "quote_volume_24h", a.quote_volume, a.quote_volume is not None,
        "volume 24h en quote (réel exchange)", as_of=as_of_ms, age=age_ms)
    add(src, "num_trades_24h", a.num_trades, a.num_trades is not None,
        f"{a.num_trades:,} transactions réelles sur 24h".replace(",", " ") if a.num_trades else "",
        as_of=as_of_ms, age=age_ms)
    add(src, "high_low_24h", [a.low_24h, a.high_24h],
        a.high_24h is not None and a.low_24h is not None,
        "bornes réelles du range 24h (base des niveaux d'invalidation/TP/SL)",
        as_of=as_of_ms, age=age_ms)
    add(src, "vwap_24h", a.vwap_24h, a.vwap_24h is not None,
        "prix moyen pondéré par les volumes (réel)", as_of=as_of_ms, age=age_ms)
    add(src, "spread_bps", a.spread_bps, a.spread_bps is not None,
        "" if a.spread_bps is not None else
        "Donnée indisponible — carnet live réservé au symbole sélectionné (Tier 3)")
    # Macro context (shared, real-data-only)
    add("global_context", "market_regime", ctx.regime, True,
        "régime dérivé de largeur de marché + Fear&Greed + variation mcap 24h")
    add("global_context", "fear_greed", ctx.fear_greed, ctx.fear_greed is not None,
        "" if ctx.fear_greed is not None else "Donnée indisponible — source alternative.me non répondue")
    add("global_context", "btc_change_24h", ctx.btc_change_24h, ctx.btc_change_24h is not None,
        "référence de force relative")
    # Knowingly-unavailable inputs surfaced honestly (no fabrication).
    add("unavailable", "change_1h_7d_30d", None, False,
        "Donnée indisponible — horizons non fournis par le ticker 24h ; la confiance du modèle est plafonnée en conséquence")
    add("unavailable", "market_cap", None, False,
        "Donnée indisponible — capitalisation réservée à la watchlist externe CoinGecko (top 1000)")
    return ev


def _justification(signal: str, a: AssetInput, s: AssetScores) -> str:
    """Short French justification of the signal (analyst tone)."""
    bits = []
    if s.momentum_ratio is not None:
        if s.momentum_ratio >= 0.6:
            bits.append("momentum positif")
        elif s.momentum_ratio <= 0.4:
            bits.append("momentum négatif")
        else:
            bits.append("momentum neutre")
    if s.liquidity_ratio >= 0.6:
        bits.append("bonne liquidité")
    elif s.liquidity_ratio < 0.3:
        bits.append("liquidité faible")
    if s.volume_confirmation_ratio is not None and s.volume_confirmation_ratio < 0.45:
        bits.append("volume peu confirmant")
    if s.risk_score >= 60:
        bits.append("risque élevé")
    elif s.risk_score <= 40:
        bits.append("risque contenu")
    reason = ", ".join(bits) if bits else "situation neutre"

    if signal == "BUY":
        return f"Signal BUY : {reason}. Opportunité {round(s.opportunity_score)}/100, risque {round(s.risk_score)}/100."
    if signal == "SELL":
        return f"Signal SELL : {reason}. La pression baissière domine."
    if signal == "AVOID":
        return f"Signal AVOID : {reason}. Données insuffisantes ou risque/illiquidité trop élevés."
    return f"Signal HOLD : {reason}. Pas de confirmation suffisante pour agir."


_RISK_LABELS_FR = {
    "volatility": "volatilité élevée",
    "drawdown": "drawdown en cours depuis le plus haut 24h",
    "illiquidity": "liquidité insuffisante",
    "data_quality": "données incomplètes ou périmées",
    "microstructure": "spread large (marché fragile/manipulable)",
}


def _main_risk(s: AssetScores) -> str:
    """The dominant risk component, in plain French (largest weighted term)."""
    if not s.risk_components:
        return "risque non évalué"
    key = max(s.risk_components, key=lambda k: s.risk_components[k])
    return _RISK_LABELS_FR.get(key, key)


def _rationale(sig: str, conv: str, a: AssetInput, s: AssetScores,
               contradictions: list[str]) -> str:
    """Decision-grade rationale: why the model recommends this action, with the
    metrics that triggered it and the main risk. Professional, direct tone."""
    sub = scoring.sub_scores(s)

    def n(key):
        v = sub.get(key)
        return "n.d." if v is None else f"{round(v)}/100"

    drivers = (f"momentum {n('momentum_score')}, tendance {n('trend_score')}, "
               f"volume {n('volume_score')}, liquidité {n('liquidity_score')}, "
               f"force relative vs BTC {n('relative_strength_score')}")
    risk_txt = f"Risque principal : {_main_risk(s)} (score de risque {round(s.risk_score)}/100)."
    contra_txt = (" Signaux contradictoires : " + " ; ".join(contradictions) + ".") if contradictions else ""

    if sig == "BUY":
        strength = {"forte": "de renforcer", "moyenne": "d'acheter modérément",
                    "faible": "d'initier une position réduite sur"}.get(conv, "d'acheter")
        return (f"Le modèle recommande {strength} cette position : {drivers}. "
                f"Conviction {conv} (confiance {round(s.confidence_score)}/100). {risk_txt}{contra_txt}")
    if sig == "SELL":
        verb = "de vendre" if conv == "forte" else "d'alléger"
        return (f"Le modèle recommande {verb} : momentum dégradé ({n('momentum_score')}), "
                f"drawdown {n('drawdown_score')}, détérioration de la force relative "
                f"({n('relative_strength_score')}). {risk_txt}{contra_txt}")
    if sig == "AVOID":
        return (f"Le modèle recommande d'éviter cet actif : la qualité des données ou la "
                f"structure de marché ne permet pas une position fiable ({drivers}). {risk_txt}{contra_txt}")
    return (f"Le modèle recommande de conserver/surveiller sans renforcer : {drivers}. "
            f"Le signal n'atteint pas les seuils d'action (opportunité {round(s.opportunity_score)}/100). "
            f"{risk_txt}{contra_txt}")


def _levels(sig: str, a: AssetInput) -> dict:
    """Indicative action levels derived ONLY from real observed 24h levels
    (low/high/VWAP). Clearly framed as zones, not promises; None when the
    underlying real level is missing."""
    low, high, vwap = a.low_24h, a.high_24h, a.vwap_24h
    if sig == "SELL":
        return {
            "invalidation_level": high,
            "invalidation_note": ("reprise durable au-dessus du plus haut 24h "
                                  f"({_fmt_price(high)}) — invaliderait la thèse baissière") if high is not None
                                 else "Donnée indisponible — plus haut 24h manquant",
            "take_profit_zone": low,
            "take_profit_note": f"zone du plus bas 24h ({_fmt_price(low)})" if low is not None
                                else "Donnée indisponible",
            "stop_loss_zone": vwap if vwap is not None else high,
            "stop_loss_note": "retour au-dessus du VWAP 24h (les acheteurs reprennent la main)"
                              if vwap is not None else "plus haut 24h",
        }
    return {
        "invalidation_level": low,
        "invalidation_note": ("cassure durable sous le plus bas 24h "
                              f"({_fmt_price(low)}) — invaliderait la thèse haussière") if low is not None
                             else "Donnée indisponible — plus bas 24h manquant",
        "take_profit_zone": high,
        "take_profit_note": f"zone du plus haut 24h ({_fmt_price(high)}) en première cible" if high is not None
                            else "Donnée indisponible",
        "stop_loss_zone": low,
        "stop_loss_note": "sous le plus bas 24h (support réel observé)" if low is not None
                          else "Donnée indisponible",
    }


def _explanation_simple(signal: str, a: AssetInput, s: AssetScores, pred: dict) -> str:
    """Beginner-friendly explanation in plain French."""
    sym = a.symbol
    chg = a.change_24h
    chg_txt = (f"{'monté' if (chg or 0) >= 0 else 'baissé'} de {abs(chg):.1f}% sur 24h"
               if chg is not None else "peu de variation mesurable")
    up = round(pred["up_probability"] * 100)
    if signal == "BUY":
        return (f"{sym} a {chg_txt}. Les signaux (élan, liquidité, risque) sont plutôt "
                f"favorables, avec une probabilité de hausse estimée à {up}%. "
                f"Cela ressemble à une opportunité, mais ce n'est pas une certitude.")
    if signal == "SELL":
        return (f"{sym} a {chg_txt}. L'élan est négatif et le risque est élevé : la tendance "
                f"de court terme semble s'affaiblir. Probabilité de hausse estimée à {up}% seulement.")
    if signal == "AVOID":
        return (f"{sym} a {chg_txt}, mais soit le volume est trop faible, soit l'écart de prix "
                f"(spread) est large, soit les données sont insuffisantes. Le mouvement peut être "
                f"fragile ou manipulable : prudence, mieux vaut éviter.")
    return (f"{sym} a {chg_txt}. Le signal n'est pas assez net pour acheter ou vendre : "
            f"probabilité de hausse estimée à {up}%, on reste à l'observation (HOLD).")


def _score_asset(row: dict, a: AssetInput, ctx: MarketContext, rank: int) -> dict:
    s = scoring.opportunity_score(a, ctx)
    sig = scoring.signal(a, s)
    conv = scoring.conviction(sig, s)
    contra = scoring.contradictions(a, s)
    rat = scoring.rating(s.opportunity_score, s.risk_score, s.confidence_score)
    hor = scoring.horizon(a, s)
    pred = _prediction(a, ctx, s)
    justification = _justification(sig, a, s)
    explanation = _explanation_simple(sig, a, s, pred)
    levels = _levels(sig, a)

    return {
        "rank": rank,
        "symbol": a.symbol,
        "name": a.name,
        "base": a.base,
        "price": a.price,
        # Real 24h change; other horizons are knowingly unavailable (N/A).
        "change_1h": None,
        "change_24h": a.change_24h,
        "change_7d": None,
        "change_30d": None,
        "quote_volume_24h": a.quote_volume,
        "market_cap": None,           # not available without a market-cap source
        "num_trades_24h": a.num_trades,
        "spread_bps": a.spread_bps,
        "depth_usd_10bps": None,      # L2 reserved for the selected (Tier-3) symbol
        "volume_percentile": a.volume_percentile,
        "liquidity": s.liquidity_ratio,
        "volatility": s.volatility_ratio,
        "momentum": s.momentum_ratio,
        "drawdown": s.drawdown_ratio,
        "trend_score": s.trend_quality_ratio,
        "risk_score": s.risk_score,
        "liquidity_score": round(s.liquidity_ratio * 100, 1),
        "opportunity_score": s.opportunity_score,
        "confidence_score": s.confidence_score,
        "scores": scoring.sub_scores(s),   # every 0–100 sub-score (None = N/A)
        "rating": rat,
        "signal": sig,
        "conviction": conv,
        "horizon": hor,
        "justification": justification,
        "rationale": _rationale(sig, conv, a, s, contra),
        "contradictions": contra,
        "main_risk": _main_risk(s),
        **levels,
        "metrics": {
            "momentum_ratio": s.momentum_ratio,
            "volume_confirmation_ratio": s.volume_confirmation_ratio,
            "liquidity_ratio": s.liquidity_ratio,
            "relative_strength_btc": s.relative_strength_btc,
            "trend_quality_ratio": s.trend_quality_ratio,
            "volatility_ratio": s.volatility_ratio,
            "drawdown_ratio": s.drawdown_ratio,
            "market_context_score": s.market_context_score,
            "opportunity_components": s.components,
            "risk_components": s.risk_components,
        },
        "prediction": pred,
        "explanation_simple": explanation,
        "missing_features": scoring.missing_features(a),
        "source_evidence": _source_evidence(a, ctx, row),
        "stale": a.stale,
    }


def _slim(asset: dict) -> dict:
    return {k: asset.get(k) for k in _SLIM}


# ──────────────────────────────────────────────────────────────────────────────
# Top-level report
# ──────────────────────────────────────────────────────────────────────────────

def _data_quality(assets: list[dict], global_context: Optional[dict],
                  external_watchlist: Optional[dict]) -> dict:
    """Honest summary of what the model could and could not see today."""
    n = max(len(assets), 1)
    core = len(scoring._CORE_FIELDS)
    completeness = [1.0 - min(len([m for m in a.get("missing_features", [])
                                   if m in scoring._CORE_FIELDS]) / core, 1.0)
                    for a in assets]
    stale = sum(1 for a in assets if a.get("stale"))
    missing_spread = sum(1 for a in assets if a.get("spread_bps") is None)
    low_conf = sum(1 for a in assets if (a.get("confidence_score") or 0) < scoring.AVOID_CONF_MIN)
    gc = global_context or {}
    sources = {
        "binance_universe": {"real": bool(assets), "assets": len(assets)},
        "coingecko_global": {"real": bool((gc.get("market") or {}).get("real"))},
        "defillama": {"real": bool((gc.get("defi") or {}).get("real"))},
        "fear_greed": {"real": bool((gc.get("sentiment") or {}).get("real"))},
        "coingecko_top1000": {"status": (external_watchlist or {}).get("status", "unavailable")},
    }
    return {
        "avg_completeness_pct": round(100.0 * sum(completeness) / n, 1) if assets else 0.0,
        "stale_assets": stale,
        "assets_missing_spread": missing_spread,
        "low_confidence_assets": low_conf,
        "known_gaps": ["change_1h", "change_7d", "change_30d", "market_cap (univers)", "depth_l2 (hors symbole sélectionné)"],
        "sources": sources,
        "note": ("Chaque donnée manquante réduit le score de confiance de l'actif concerné ; "
                 "trop de données manquantes force le signal AVOID."),
    }


def compare_with_previous(assets: list[dict], previous_report: Optional[dict],
                          *, max_items: int = 20) -> Optional[dict]:
    """Diff vs the previous report: signal upgrades/downgrades, confidence
    drops, entries/exits. None when there is no real previous report."""
    if not previous_report or not previous_report.get("assets"):
        return None
    prev = {a.get("symbol"): a for a in previous_report.get("assets", [])}
    cur = {a.get("symbol"): a for a in assets}
    upgrades, downgrades, conf_drops = [], [], []
    order = {"AVOID": 0, "SELL": 1, "HOLD": 2, "BUY": 3}
    for sym, a in cur.items():
        p = prev.get(sym)
        if not p:
            continue
        s_new, s_old = a.get("signal"), p.get("signal")
        if s_new != s_old and s_new in order and s_old in order:
            item = {"symbol": sym, "from": s_old, "to": s_new,
                    "opportunity_score": a.get("opportunity_score")}
            (upgrades if order[s_new] > order[s_old] else downgrades).append(item)
        c_new, c_old = a.get("confidence_score") or 0, p.get("confidence_score") or 0
        if c_old - c_new >= 15:
            conf_drops.append({"symbol": sym, "from": round(c_old, 1), "to": round(c_new, 1)})
    new_symbols = sorted(set(cur) - set(prev))
    dropped_symbols = sorted(set(prev) - set(cur))
    return {
        "previous_report_date": previous_report.get("report_date"),
        "signal_upgrades": upgrades[:max_items],
        "signal_downgrades": downgrades[:max_items],
        "confidence_drops": conf_drops[:max_items],
        "new_symbols": new_symbols[:max_items],
        "dropped_symbols": dropped_symbols[:max_items],
        "new_symbols_count": len(new_symbols),
        "dropped_symbols_count": len(dropped_symbols),
    }


def build_daily_report(universe_rows: list[dict], global_context: Optional[dict] = None,
                       *, generated_at: str, report_date: Optional[str] = None,
                       btc_symbol: str = "BTC/USDT", top_n: int = 10,
                       previous_report: Optional[dict] = None,
                       external_watchlist: Optional[dict] = None) -> dict:
    """Build the full structured report from real universe rows + macro context."""
    inputs = [_row_to_input(r) for r in universe_rows]
    _fill_volume_percentiles(inputs)
    ctx = _build_context(inputs, global_context, btc_symbol)

    # Score, then rank by opportunity score (desc); ties broken by lower risk.
    scored = []
    for row, a in zip(universe_rows, inputs):
        scored.append((a, row))
    scored_assets = [
        _score_asset(row, a, ctx, rank=0)
        for (a, row) in scored
    ]
    scored_assets.sort(key=lambda x: (x["opportunity_score"], -x["risk_score"]), reverse=True)
    for i, asset in enumerate(scored_assets):
        asset["rank"] = i + 1

    counts = {"BUY": 0, "HOLD": 0, "SELL": 0, "AVOID": 0}
    rating_dist = {r["rating"]: 0 for r in scoring.RATING_SCALE}
    for asset in scored_assets:
        counts[asset["signal"]] = counts.get(asset["signal"], 0) + 1
        rating_dist[asset["rating"]] = rating_dist.get(asset["rating"], 0) + 1

    # Portfolio layer: posture, model allocations, per-asset actions/weights.
    portfolio = portfolio_advisor.build_portfolio_advice(
        scored_assets, regime=ctx.regime, breadth_pct=ctx.breadth_pct,
        fear_greed=ctx.fear_greed)
    weights = portfolio.get("weights_by_symbol", {})
    for asset in scored_assets:
        asset["action"] = portfolio_advisor.action_for(
            asset["signal"], asset["conviction"], asset["opportunity_score"])
        asset["cap_tier"] = portfolio_advisor.cap_tier(
            asset.get("base", ""), asset.get("volume_percentile"))
        w = weights.get(asset["symbol"]) or {}
        asset["recommended_weights"] = {
            "prudent": w.get("prudent", 0.0),
            "equilibre": w.get("equilibre", 0.0),
            "agressif": w.get("agressif", 0.0),
        }
        asset["max_weight"] = {
            p: portfolio_advisor.max_weight_for(p, asset["cap_tier"],
                                                asset.get("liquidity") or 0.0,
                                                asset.get("volatility") or 0.0)
            for p in portfolio_advisor.PROFILES
        }

    buys = [a for a in scored_assets if a["signal"] == "BUY"]
    sells = [a for a in scored_assets if a["signal"] == "SELL"]
    holds = [a for a in scored_assets if a["signal"] == "HOLD"]

    top_buy = [_slim(a) for a in buys[:top_n]]
    top_sell = [_slim(a) for a in sorted(sells, key=lambda x: x["risk_score"], reverse=True)[:top_n]]
    # Watchlist: strong HOLDs (near a BUY) worth monitoring.
    watch = sorted(holds, key=lambda x: x["opportunity_score"], reverse=True)
    top_watchlist = [_slim(a) for a in watch[:top_n]]
    top_opportunities = [_slim(a) for a in scored_assets[:top_n]]
    top_risks = [_slim(a) for a in sorted(scored_assets, key=lambda x: x["risk_score"], reverse=True)[:top_n]]

    confs = [a["confidence_score"] for a in scored_assets if a.get("confidence_score") is not None]
    model_confidence = round(sum(confs) / len(confs), 1) if confs else None

    summary_text = _executive_summary_text(ctx, counts, scored_assets, top_buy, top_sell, portfolio)
    data_quality = _data_quality(scored_assets, global_context, external_watchlist)
    changes = compare_with_previous(scored_assets, previous_report)

    return {
        "schema_version": 2,
        "report_kind": REPORT_KIND,
        "report_date": report_date,
        "generated_at": generated_at,
        "universe_size": len(scored_assets),
        "btc_reference": btc_symbol,
        "market_regime": ctx.regime,
        "market_context": {
            "regime": ctx.regime,
            "breadth_pct": ctx.breadth_pct,
            "btc_change_24h": ctx.btc_change_24h,
            "fear_greed": ctx.fear_greed,
            "mcap_change_24h": ctx.mcap_change_24h,
        },
        "executive_summary": {
            "regime": ctx.regime,
            "regime_label": _REGIME_FR.get(ctx.regime, "neutre"),
            "posture": portfolio.get("posture"),
            "posture_label": portfolio.get("posture_label"),
            "global_conviction": portfolio.get("global_conviction"),
            "model_confidence": model_confidence,
            "signal_counts": counts,
            "top_opportunities": top_opportunities[:5],
            "top_risks": top_risks[:5],
            "generated_at": generated_at,
        },
        "summary": summary_text,
        "signal_counts": counts,
        "rating_distribution": rating_dist,
        "rating_scale": scoring.RATING_SCALE,
        "portfolio_models": portfolio,
        "top_buy": top_buy,
        "top_sell": top_sell,
        "top_watchlist": top_watchlist,
        "top_opportunities": top_opportunities,
        "top_risks": top_risks,
        "assets": scored_assets,
        "watchlist_external": external_watchlist or {
            "status": "unavailable", "source": "coingecko",
            "reason": "non récupérée pour cette génération"},
        "data_quality": data_quality,
        "changes_vs_previous": changes,
        "disclaimer": DISCLAIMER,
        "data_sources": {
            "universe": "binance_spot (24h ticker)",
            "macro": "coingecko + defillama + alternative.me",
            "external_watchlist": "coingecko /coins/markets (top 1000)",
            "note": "Horizons 1h/7j/30j et market cap univers non disponibles → 'Donnée indisponible'.",
        },
    }


_REGIME_FR = {"bullish": "haussier", "neutral": "neutre", "bearish": "baissier"}


def _executive_summary_text(ctx: MarketContext, counts: dict, assets: list[dict],
                            top_buy: list[dict], top_sell: list[dict],
                            portfolio: Optional[dict] = None) -> str:
    regime_fr = _REGIME_FR.get(ctx.regime, "neutre")
    parts = [f"Régime de marché : **{regime_fr}**."]
    if portfolio:
        parts.append(f"Positionnement recommandé : **{portfolio.get('posture_label', '?')}** "
                     f"(conviction du modèle : {portfolio.get('global_conviction', '?')}).")
    if ctx.breadth_pct is not None:
        parts.append(f"Largeur de marché : {round(ctx.breadth_pct*100)} % des {len(assets)} cryptos suivies en hausse 24h.")
    if ctx.fear_greed is not None:
        parts.append(f"Fear & Greed : {round(ctx.fear_greed)}.")
    parts.append(f"Signaux : {counts.get('BUY',0)} BUY, {counts.get('HOLD',0)} HOLD, "
                 f"{counts.get('SELL',0)} SELL, {counts.get('AVOID',0)} AVOID.")
    if top_buy:
        names = ", ".join(b["symbol"] for b in top_buy[:3])
        parts.append(f"Principales opportunités : {names}.")
    else:
        parts.append("Aucune opportunité d'achat ne franchit les seuils du modèle : "
                     "positionnement prudent, préserver le capital.")
    if top_sell:
        names = ", ".join(b["symbol"] for b in top_sell[:3])
        parts.append(f"Allègements/ventes recommandés : {names}.")
    return " ".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Markdown render (human-readable French)
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_usd(n: Optional[float]) -> str:
    if n is None:
        return "N/A"
    a = abs(n)
    if a >= 1e12:
        return f"${n/1e12:.2f}T"
    if a >= 1e9:
        return f"${n/1e9:.2f}B"
    if a >= 1e6:
        return f"${n/1e6:.2f}M"
    if a >= 1e3:
        return f"${n/1e3:.1f}K"
    return f"${n:.2f}"


def _fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "N/A"
    a = abs(p)
    dp = 2 if a >= 1000 else 3 if a >= 1 else 6
    return f"{p:,.{dp}f}"


def _fmt_pct(p: Optional[float]) -> str:
    return "N/A" if p is None else f"{p:+.2f}%"


def render_markdown(report: dict) -> str:
    """Render the report as readable French Markdown (reports/daily_*.md)."""
    L: list[str] = []
    date = report.get("report_date") or report.get("generated_at", "")[:10]
    regime_fr = _REGIME_FR.get(report.get("market_regime", "neutral"), "neutre")
    counts = report.get("signal_counts", {})

    L.append(f"# Rapport Crypto Quotidien — {date}")
    L.append("")
    L.append(f"> _{report.get('report_kind', REPORT_KIND)}._")
    L.append(f"> _Généré le {report.get('generated_at','')}. Univers : "
             f"{report.get('universe_size',0)} cryptos (Binance Spot)._")
    L.append("")
    L.append(f"> {report.get('disclaimer','')}")
    L.append("")

    # 1. Executive summary
    ex = report.get("executive_summary", {})
    L.append("## 1. Résumé exécutif")
    L.append("")
    L.append(f"- **Régime de marché** : {regime_fr}")
    if ex.get("posture_label"):
        L.append(f"- **Positionnement recommandé** : {ex['posture_label']}")
    if ex.get("global_conviction"):
        L.append(f"- **Conviction du modèle** : {ex['global_conviction']}")
    if ex.get("model_confidence") is not None:
        L.append(f"- **Confiance moyenne du modèle** : {round(ex['model_confidence'])}/100")
    mc = report.get("market_context", {})
    if mc.get("breadth_pct") is not None:
        L.append(f"- **Largeur de marché** : {round(mc['breadth_pct']*100)}% des cryptos en hausse 24h")
    if mc.get("fear_greed") is not None:
        L.append(f"- **Fear & Greed** : {round(mc['fear_greed'])}")
    L.append(f"- **Signaux** : {counts.get('BUY',0)} BUY · {counts.get('HOLD',0)} HOLD · "
             f"{counts.get('SELL',0)} SELL · {counts.get('AVOID',0)} AVOID")
    L.append("")
    L.append(report.get("summary", ""))
    L.append("")

    # 1bis. Model portfolio
    pf = report.get("portfolio_models") or {}
    if pf.get("profiles"):
        L.append("## Portefeuille modèle")
        L.append("")
        L.append(pf.get("posture_justification", ""))
        L.append("")
        labels = pf.get("bucket_labels", {})
        L.append("| Profil | " + " | ".join(labels.get(b, b) for b in
                 ("btc_eth", "large_caps", "mid_caps", "small_caps", "stables_cash", "opportunistic"))
                 + " | Risque | Drawdown estimé | Horizon |")
        L.append("|---" * 9 + "|")
        for key, prof in pf["profiles"].items():
            alloc = prof.get("allocation", {})
            L.append(f"| **{prof.get('label', key)}** | "
                     + " | ".join(f"{alloc.get(b, 0)}%" for b in
                                  ("btc_eth", "large_caps", "mid_caps", "small_caps", "stables_cash", "opportunistic"))
                     + f" | {prof.get('risk_level','')} | {prof.get('expected_drawdown','')} | {prof.get('horizon','')} |")
        L.append("")
        L.append(f"_{pf.get('cap_tier_note','')}_")
        L.append("")

    # Top lists
    def _toplist(title, rows):
        L.append(f"### {title}")
        L.append("")
        if not rows:
            L.append("_Aucune entrée._")
            L.append("")
            return
        L.append("| # | Symbole | Signal | Rating | Opp. | Risque | Conf. | 24h |")
        L.append("|---|---------|--------|--------|------|--------|-------|-----|")
        for r in rows:
            L.append(f"| {r.get('rank','')} | {r.get('symbol','')} | {r.get('signal','')} | "
                     f"{r.get('rating','')} | {round(r.get('opportunity_score') or 0)} | "
                     f"{round(r.get('risk_score') or 0)} | {round(r.get('confidence_score') or 0)} | "
                     f"{_fmt_pct(r.get('change_24h'))} |")
        L.append("")

    _toplist("Top opportunités (BUY)", report.get("top_buy", []))
    _toplist("Top risques de baisse (SELL)", report.get("top_sell", []))
    _toplist("À surveiller (watchlist)", report.get("top_watchlist", []))

    # 2. Rating distribution + scale
    L.append("## 2. Distribution des ratings")
    L.append("")
    dist = report.get("rating_distribution", {})
    L.append("| Rating | Compte | Définition |")
    L.append("|--------|--------|------------|")
    for item in report.get("rating_scale", scoring.RATING_SCALE):
        r = item["rating"]
        L.append(f"| {r} | {dist.get(r,0)} | {item['definition']} |")
    L.append("")

    # 3. Global ranking (capped for readability; full data is in the JSON)
    L.append("## 3. Classement global (top 50)")
    L.append("")
    L.append("| # | Symbole | Prix | 24h | Volume 24h | Signal | Action | Conviction | Rating | Opp. | Risque | Conf. |")
    L.append("|---|---------|------|-----|-----------|--------|--------|-----------|--------|------|--------|-------|")
    for a in report.get("assets", [])[:50]:
        L.append(f"| {a['rank']} | {a['symbol']} | {_fmt_price(a.get('price'))} | "
                 f"{_fmt_pct(a.get('change_24h'))} | {_fmt_usd(a.get('quote_volume_24h'))} | "
                 f"{a['signal']} | {a.get('action','')} | {a.get('conviction','')} | {a['rating']} | "
                 f"{round(a['opportunity_score'])} | {round(a['risk_score'])} | {round(a['confidence_score'])} |")
    L.append("")
    if len(report.get("assets", [])) > 50:
        L.append(f"_… {len(report['assets']) - 50} autres cryptos dans le rapport JSON complet._")
        L.append("")

    # 4. Spotlight on top recommendations (decision-grade)
    L.append("## 4. Focus recommandations")
    L.append("")
    spotlight = (report.get("top_buy") or report.get("top_opportunities") or [])[:3]
    full_by_symbol = {a["symbol"]: a for a in report.get("assets", [])}
    for slim in spotlight:
        a = full_by_symbol.get(slim["symbol"], slim)
        L.append(f"### {a['symbol']} — {a.get('signal','')} / {a.get('action','')} (rating {a.get('rating','')}, conviction {a.get('conviction','')})")
        L.append("")
        L.append(a.get("rationale") or a.get("explanation_simple", ""))
        L.append("")
        pred = a.get("prediction", {})
        if pred:
            L.append(f"- **Probabilité de hausse (indicative)** : {round((pred.get('up_probability') or 0)*100)}% "
                     f"· confiance {pred.get('confidence_level','?')}")
            L.append(f"- **Scénario haussier** : {pred.get('bullish_case','')}")
            L.append(f"- **Scénario baissier** : {pred.get('bearish_case','')}")
        if a.get("invalidation_note"):
            L.append(f"- **Invalidation** : {a['invalidation_note']}")
        if a.get("take_profit_note"):
            L.append(f"- **Take profit indicatif** : {a['take_profit_note']}")
        if a.get("stop_loss_note"):
            L.append(f"- **Stop loss indicatif** : {a['stop_loss_note']}")
        L.append("")

    # 5. External top-1000 watchlist
    wl = report.get("watchlist_external") or {}
    L.append("## 5. Watchlist externe (top 1000 CoinGecko)")
    L.append("")
    if wl.get("status") in ("ok", "partial"):
        L.append(f"- Suivies dans l'app : {wl.get('tracked_count', 0)} · hors app : {wl.get('untracked_count', 0)} "
                 f"· exclues (liquidité/données) : {wl.get('excluded_count', 0)}")
        opps = wl.get("new_opportunities") or []
        if opps:
            L.append("")
            L.append("| Rang mcap | Symbole | Nom | Prix | 24h | Volume 24h | Market cap |")
            L.append("|---|---|---|---|---|---|---|")
            for o in opps[:15]:
                L.append(f"| {o.get('market_cap_rank','?')} | {o.get('base','')} | {o.get('name','')} | "
                         f"{_fmt_price(o.get('price'))} | {_fmt_pct(o.get('change_24h'))} | "
                         f"{_fmt_usd(o.get('volume_24h'))} | {_fmt_usd(o.get('market_cap'))} |")
        else:
            L.append("- Aucune nouvelle opportunité ne franchit le plancher de volume.")
    else:
        L.append(f"_Watchlist externe indisponible ({wl.get('reason') or wl.get('error') or wl.get('status','?')})._")
    L.append("")

    # 6. Data quality + changes
    dq = report.get("data_quality") or {}
    L.append("## 6. Qualité des données")
    L.append("")
    L.append(f"- Complétude moyenne des données : {dq.get('avg_completeness_pct','?')} %")
    L.append(f"- Actifs avec données périmées : {dq.get('stale_assets','?')} · "
             f"confiance faible : {dq.get('low_confidence_assets','?')}")
    L.append(f"- Lacunes connues : {', '.join(dq.get('known_gaps', []))}")
    L.append("")
    ch = report.get("changes_vs_previous")
    if ch:
        L.append(f"### Évolution vs rapport du {ch.get('previous_report_date','?')}")
        L.append("")
        ups = ", ".join(f"{c['symbol']} ({c['from']}→{c['to']})" for c in ch.get("signal_upgrades", [])[:8]) or "aucune"
        downs = ", ".join(f"{c['symbol']} ({c['from']}→{c['to']})" for c in ch.get("signal_downgrades", [])[:8]) or "aucune"
        L.append(f"- Améliorations de signal : {ups}")
        L.append(f"- Dégradations de signal : {downs}")
        L.append(f"- Nouvelles entrées : {ch.get('new_symbols_count', 0)} · sorties : {ch.get('dropped_symbols_count', 0)}")
        L.append("")

    L.append("---")
    L.append("")
    L.append(f"_{report.get('disclaimer','')}_")
    L.append("")
    return "\n".join(L)
