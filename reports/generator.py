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

from reports import scoring
from reports.scoring import AssetInput, MarketContext, AssetScores

DISCLAIMER = (
    "Ce rapport est généré automatiquement à partir de données de marché réelles "
    "(Binance Spot 24h + contexte macro). Il a une vocation informative et "
    "pédagogique uniquement. Ce n'est PAS un conseil financier personnalisé. "
    "Les prédictions sont indicatives, exprimées en probabilités et en scénarios, "
    "et n'ont aucune valeur de certitude. Le marché crypto est volatil et risqué : "
    "ne risquez que ce que vous pouvez vous permettre de perdre."
)

# Slim per-asset fields for the executive-summary top-lists.
_SLIM = ("rank", "symbol", "name", "signal", "rating", "opportunity_score",
         "risk_score", "confidence_score", "price", "change_24h", "justification",
         "explanation_simple")


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


def _source_evidence(a: AssetInput, ctx: MarketContext, row: dict) -> list[dict]:
    """Real, traceable evidence — only fields that actually exist. Never the
    'No real source evidence available' placeholder unless the asset is empty."""
    src = row.get("source") or "binance_spot"
    ev: list[dict] = []

    def add(source, metric, value, available, note=""):
        ev.append({"source": source, "metric": metric, "value": value,
                   "available": available, "note": note})

    add(src, "price", a.price, a.price is not None)
    add(src, "change_24h_pct", a.change_24h, a.change_24h is not None)
    add(src, "quote_volume_24h", a.quote_volume, a.quote_volume is not None)
    add(src, "num_trades_24h", a.num_trades, a.num_trades is not None)
    add(src, "high_low_24h", [a.low_24h, a.high_24h],
        a.high_24h is not None and a.low_24h is not None)
    add(src, "vwap_24h", a.vwap_24h, a.vwap_24h is not None)
    add(src, "spread_bps", a.spread_bps, a.spread_bps is not None,
        "" if a.spread_bps is not None else "carnet live réservé au symbole sélectionné")
    # Macro context (shared, real-data-only)
    add("global_context", "market_regime", ctx.regime, True)
    add("global_context", "fear_greed", ctx.fear_greed, ctx.fear_greed is not None)
    add("global_context", "btc_change_24h", ctx.btc_change_24h, ctx.btc_change_24h is not None)
    # Knowingly-unavailable inputs surfaced honestly (no fabrication).
    add("unavailable", "change_1h_7d_30d", None, False, "horizons non fournis par le ticker 24h")
    add("unavailable", "market_cap", None, False, "capitalisation non disponible sans agrégateur (CoinGecko)")
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
    rat = scoring.rating(s.opportunity_score, s.risk_score, s.confidence_score)
    hor = scoring.horizon(a, s)
    pred = _prediction(a, ctx, s)
    justification = _justification(sig, a, s)
    explanation = _explanation_simple(sig, a, s, pred)

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
        "liquidity": s.liquidity_ratio,
        "volatility": s.volatility_ratio,
        "momentum": s.momentum_ratio,
        "drawdown": s.drawdown_ratio,
        "trend_score": s.trend_quality_ratio,
        "risk_score": s.risk_score,
        "liquidity_score": round(s.liquidity_ratio * 100, 1),
        "opportunity_score": s.opportunity_score,
        "confidence_score": s.confidence_score,
        "rating": rat,
        "signal": sig,
        "horizon": hor,
        "justification": justification,
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

def build_daily_report(universe_rows: list[dict], global_context: Optional[dict] = None,
                       *, generated_at: str, report_date: Optional[str] = None,
                       btc_symbol: str = "BTC/USDT", top_n: int = 10) -> dict:
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

    summary_text = _executive_summary_text(ctx, counts, scored_assets, top_buy, top_sell)

    return {
        "schema_version": 1,
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
        "summary": summary_text,
        "signal_counts": counts,
        "rating_distribution": rating_dist,
        "rating_scale": scoring.RATING_SCALE,
        "top_buy": top_buy,
        "top_sell": top_sell,
        "top_watchlist": top_watchlist,
        "top_opportunities": top_opportunities,
        "top_risks": top_risks,
        "assets": scored_assets,
        "disclaimer": DISCLAIMER,
        "data_sources": {
            "universe": "binance_spot (24h ticker)",
            "macro": "coingecko + defillama + alternative.me",
            "note": "Horizons 1h/7j/30j et market cap non disponibles → N/A.",
        },
    }


_REGIME_FR = {"bullish": "haussier", "neutral": "neutre", "bearish": "baissier"}


def _executive_summary_text(ctx: MarketContext, counts: dict, assets: list[dict],
                            top_buy: list[dict], top_sell: list[dict]) -> str:
    regime_fr = _REGIME_FR.get(ctx.regime, "neutre")
    parts = [f"Le marché crypto est globalement **{regime_fr}** aujourd'hui."]
    if ctx.breadth_pct is not None:
        parts.append(f"{round(ctx.breadth_pct*100)}% des {len(assets)} cryptos suivies sont en hausse sur 24h.")
    if ctx.fear_greed is not None:
        parts.append(f"L'indice Fear & Greed est à {round(ctx.fear_greed)}.")
    parts.append(f"Répartition des signaux : {counts.get('BUY',0)} BUY, "
                 f"{counts.get('HOLD',0)} HOLD, {counts.get('SELL',0)} SELL, "
                 f"{counts.get('AVOID',0)} AVOID.")
    if top_buy:
        names = ", ".join(b["symbol"] for b in top_buy[:3])
        parts.append(f"Principales opportunités : {names}.")
    else:
        parts.append("Aucune opportunité d'achat franche ne se dégage : prudence générale.")
    if top_sell:
        names = ", ".join(b["symbol"] for b in top_sell[:3])
        parts.append(f"À surveiller au risque de baisse : {names}.")
    parts.append("Rappel : analyse indicative, ce n'est pas un conseil financier personnalisé.")
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
    L.append(f"> _Généré le {report.get('generated_at','')}. Univers : "
             f"{report.get('universe_size',0)} cryptos (Binance Spot)._")
    L.append("")
    L.append(f"> ⚠️ **{report.get('disclaimer','')}**")
    L.append("")

    # 1. Executive summary
    L.append("## 1. Résumé exécutif")
    L.append("")
    L.append(f"- **Tendance générale du marché** : {regime_fr}")
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
    L.append("| # | Symbole | Prix | 24h | Volume 24h | Signal | Rating | Opp. | Risque | Conf. | Horizon |")
    L.append("|---|---------|------|-----|-----------|--------|--------|------|--------|-------|---------|")
    for a in report.get("assets", [])[:50]:
        L.append(f"| {a['rank']} | {a['symbol']} | {_fmt_price(a.get('price'))} | "
                 f"{_fmt_pct(a.get('change_24h'))} | {_fmt_usd(a.get('quote_volume_24h'))} | "
                 f"{a['signal']} | {a['rating']} | {round(a['opportunity_score'])} | "
                 f"{round(a['risk_score'])} | {round(a['confidence_score'])} | {a['horizon']} |")
    L.append("")
    if len(report.get("assets", [])) > 50:
        L.append(f"_… {len(report['assets']) - 50} autres cryptos dans le rapport JSON complet._")
        L.append("")

    # 4. Spotlight on top opportunities (pedagogical)
    L.append("## 4. Focus pédagogique")
    L.append("")
    spotlight = (report.get("top_buy") or report.get("top_opportunities") or [])[:3]
    full_by_symbol = {a["symbol"]: a for a in report.get("assets", [])}
    for slim in spotlight:
        a = full_by_symbol.get(slim["symbol"], slim)
        L.append(f"### {a['symbol']} — {a.get('signal','')} (rating {a.get('rating','')})")
        L.append("")
        L.append(a.get("explanation_simple", ""))
        L.append("")
        pred = a.get("prediction", {})
        if pred:
            L.append(f"- **Probabilité de hausse (indicative)** : {round((pred.get('up_probability') or 0)*100)}% "
                     f"· confiance {pred.get('confidence_level','?')}")
            L.append(f"- **Scénario** : {pred.get('scenario','')}")
            L.append(f"- **Invalidation** : {pred.get('invalidation','')}")
        L.append("")

    L.append("---")
    L.append("")
    L.append(f"_{report.get('disclaimer','')}_")
    L.append("")
    return "\n".join(L)
