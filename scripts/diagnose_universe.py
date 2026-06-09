#!/usr/bin/env python3
"""Diagnostic « pourquoi pas N cryptos ? » pour l'univers Tier-1.

Répond à la question : *j'ai demandé le top-N (ex. 300) mais combien sont
réellement chargées, et pour celles qui manquent — pourquoi exactement ?*

Il consomme l'endpoint **déjà existant** `GET /api/market/universe/debug`
(source de vérité, alimentée par `market/universe.py`) et présente un rapport
lisible : demandé vs éligible vs chargé, et la **partition des rejets**
(stable / leverage / volume / not-spot / inactive / quote-mismatch), avec des
exemples de symboles par raison.

L'univers est un tier **display-only** (flux Binance `!ticker@arr` en mémoire,
séparé de la persistance) ; ce diagnostic n'interroge donc PAS la DB — il
reflète exactement ce que le cockpit voit.

Usage :
    python scripts/diagnose_universe.py --limit 300
    python scripts/diagnose_universe.py --base-url http://127.0.0.1:8000 --json
    python scripts/diagnose_universe.py --strict      # exit 1 si chargé < demandé

Stdlib uniquement (urllib) — aucun import du projet, lançable seul.
Exit codes : 0 = OK (ou diagnostic affiché) ; 1 = (--strict) chargé < demandé ;
2 = API injoignable / réponse invalide.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# Raisons d'exclusion telles que partitionnées par market/universe.rejection_reason().
# (ordre = ordre d'évaluation ; chaque rejet compte pour UNE seule raison)
EXCLUSION_KEYS = (
    ("excluded_not_spot_count", "not_spot", "paire non SPOT/TRADING (exchangeInfo)"),
    ("excluded_inactive_count", "inactive", "marché inactif / ticker périmé"),
    ("excluded_stable_count", "stable", "stablecoin / fiat exclu"),
    ("excluded_leverage_count", "leverage", "leverage token (UP/DOWN/BULL/BEAR/3L/3S)"),
    ("excluded_low_volume_count", "low_volume", "volume 24h < plancher liquidité"),
)


def fetch_debug(base_url: str, timeout: float) -> dict:
    """GET /api/market/universe/debug → dict. Lève sur erreur réseau/HTTP/JSON."""
    url = base_url.rstrip("/") + "/api/market/universe/debug"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def summarize(debug: dict, requested: int | None = None) -> dict:
    """
    Réduit le payload debug à un verdict structuré (fonction PURE, testée offline).

    Renvoie : counts clés, partition des exclusions, manquants vs demandé, une
    cause dominante et une recommandation actionnable.
    """
    req = int(requested if requested is not None else debug.get("requested_limit") or 0)
    raw = int(debug.get("raw_binance_tickers_count") or 0)
    eligible = int(debug.get("eligible_symbols_count") or 0)
    final = int(debug.get("final_universe_count") or 0)
    capped = int(debug.get("capped_by_limit") or 0)
    quote_mismatch = int(debug.get("quote_mismatch_count") or 0)
    last_error = debug.get("last_error")

    exclusions = []
    for key, name, label in EXCLUSION_KEYS:
        exclusions.append({"reason": name, "label": label, "count": int(debug.get(key) or 0)})
    exclusions_total = sum(e["count"] for e in exclusions)

    missing_vs_requested = max(0, req - final)

    # — Cause dominante du déficit —
    if raw == 0:
        cause = "rest_unavailable"
        recommendation = (
            "Binance REST n'a renvoyé aucun ticker (raw=0). Vérifier la connectivité "
            "sortante / le champ last_error ; l'univers garde son dernier snapshot."
        )
    elif final >= req:
        cause = "none"
        recommendation = "Univers plein : chargé ≥ demandé. Rien à corriger."
    elif eligible >= req and capped > 0:
        cause = "capped_by_limit"
        recommendation = (
            f"{eligible} éligibles ≥ {req} demandés mais coupé à {final} : "
            "augmenter UNIVERSE_LIMIT / BACKEND_MAX_SYMBOLS si l'on veut plus."
        )
    else:
        # Pas assez d'éligibles : attribuer au plus gros bucket d'exclusion.
        top = max(exclusions, key=lambda e: e["count"]) if exclusions_total else None
        if top and top["reason"] == "low_volume":
            cause = "low_volume_floor"
            recommendation = (
                "Le plancher de liquidité (MIN_QUOTE_VOLUME) écarte le plus de paires : "
                "le baisser élargit l'univers (au prix de paires moins liquides)."
            )
        elif top and top["count"] > 0:
            cause = f"dominated_by_{top['reason']}"
            recommendation = (
                f"Déficit dominé par « {top['label']} » ({top['count']} paires). "
                "Ajuster le filtre correspondant si ces paires doivent entrer."
            )
        else:
            cause = "insufficient_market"
            recommendation = (
                "Trop peu de paires éligibles sur le marché actuel pour atteindre la cible "
                f"({eligible} < {req}) — limite du marché, pas un bug."
            )

    return {
        "requested": req,
        "raw_binance_tickers": raw,
        "eligible": eligible,
        "loaded": final,
        "capped_by_limit": capped,
        "quote_mismatch": quote_mismatch,
        "exclusions": exclusions,
        "exclusions_total": exclusions_total,
        "missing_vs_requested": missing_vs_requested,
        "cause": cause,
        "recommendation": recommendation,
        "last_error": last_error,
        "cache_age_ms": debug.get("cache_age_ms"),
        "build_latency_ms": debug.get("build_latency_ms"),
        "connected": debug.get("connected"),
    }


def render_text(summary: dict, debug: dict) -> str:
    out: list[str] = []
    out.append("=" * 64)
    out.append("  DIAGNOSTIC UNIVERS (Tier-1, display-only Binance !ticker@arr)")
    out.append("=" * 64)
    out.append(f"  Demandé (limit)      : {summary['requested']}")
    out.append(f"  Chargé (universe)    : {summary['loaded']}")
    out.append(f"  Éligible (avant cap) : {summary['eligible']}")
    out.append(f"  Manquant vs demandé  : {summary['missing_vs_requested']}")
    out.append(f"  Tickers Binance bruts: {summary['raw_binance_tickers']}")
    out.append(f"  Coupé par la limite  : {summary['capped_by_limit']}")
    out.append(f"  Quote ≠ {debug.get('quote_asset', '?'):<12}: {summary['quote_mismatch']}")
    out.append("")
    out.append("  Rejets (partition — chaque paire compte pour 1 raison) :")
    examples = debug.get("rejected_examples") or {}
    for e in summary["exclusions"]:
        ex = ", ".join(examples.get(e["reason"], [])[:5])
        ex = f"  e.g. {ex}" if ex else ""
        out.append(f"    - {e['reason']:<12} {e['count']:>5}   ({e['label']}){ex}")
    out.append("")
    if summary["last_error"]:
        out.append(f"  ⚠ last_error: {summary['last_error']}")
    age = summary.get("cache_age_ms")
    out.append(f"  Snapshot: connected={summary.get('connected')} "
               f"cache_age_ms={age} build_latency_ms={summary.get('build_latency_ms')}")
    out.append("")
    out.append(f"  → Cause: {summary['cause']}")
    out.append(f"  → {summary['recommendation']}")
    out.append("=" * 64)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Diagnostique le remplissage de l'univers Tier-1.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cible demandée (défaut : requested_limit renvoyé par l'API)")
    p.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base de l'API cockpit")
    p.add_argument("--timeout", type=float, default=8.0, help="Timeout HTTP (s)")
    p.add_argument("--json", action="store_true", help="Sortie JSON brute du verdict")
    p.add_argument("--strict", action="store_true", help="exit 1 si chargé < demandé")
    ns = p.parse_args(argv)

    try:
        debug = fetch_debug(ns.base_url, ns.timeout)
    except urllib.error.URLError as e:
        print(f"diagnose_universe: API injoignable sur {ns.base_url} — {e}\n"
              f"  → lancer le stack (scripts/dev_supervisor.py) puis réessayer.",
              file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as e:
        print(f"diagnose_universe: réponse invalide — {e}", file=sys.stderr)
        return 2

    summary = summarize(debug, requested=ns.limit)

    if ns.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(render_text(summary, debug))

    if ns.strict and summary["loaded"] < summary["requested"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
