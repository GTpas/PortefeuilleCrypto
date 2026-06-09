#!/usr/bin/env python3
"""Micro-bench de latence des endpoints d'affichage (univers / klines).

Mesure côté client la latence des endpoints **déjà existants** servant le
cockpit, pour vérifier que le snapshot batch est rapide et que le chart répond.
N'ouvre aucune connexion DB ; tape l'API HTTP comme le ferait le frontend.

Usage :
    python scripts/benchmark_snapshot_api.py --runs 30
    python scripts/benchmark_snapshot_api.py --symbol BTC/USDT --range 1D --runs 50

Stdlib uniquement (urllib). Exit 0 toujours (rapport), 2 si l'API est injoignable.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def percentile(samples: list[float], pct: float) -> float:
    """Percentile (interpolation linéaire) — fonction PURE, testée offline."""
    if not samples:
        return 0.0
    if len(samples) == 1:
        return samples[0]
    s = sorted(samples)
    rank = (pct / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def summarize_latencies(samples_ms: list[float]) -> dict:
    """Réduit une liste de latences (ms) à min/p50/p95/p99/max/avg (PURE)."""
    if not samples_ms:
        return {"n": 0, "min": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "avg": 0.0}
    return {
        "n": len(samples_ms),
        "min": round(min(samples_ms), 1),
        "p50": round(percentile(samples_ms, 50), 1),
        "p95": round(percentile(samples_ms, 95), 1),
        "p99": round(percentile(samples_ms, 99), 1),
        "max": round(max(samples_ms), 1),
        "avg": round(sum(samples_ms) / len(samples_ms), 1),
    }


def _time_get(url: str, timeout: float) -> tuple[float, int, int]:
    """(latence_ms, http_status, payload_bytes) pour un GET."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        status = resp.status
    return (time.perf_counter() - t0) * 1000.0, status, len(body)


def bench_endpoint(url: str, runs: int, timeout: float) -> dict:
    samples: list[float] = []
    sizes: list[int] = []
    errors = 0
    for _ in range(runs):
        try:
            ms, _status, nbytes = _time_get(url, timeout)
            samples.append(ms)
            sizes.append(nbytes)
        except urllib.error.URLError:
            errors += 1
    summary = summarize_latencies(samples)
    summary["errors"] = errors
    summary["payload_bytes_avg"] = round(sum(sizes) / len(sizes)) if sizes else 0
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bench latence des endpoints d'affichage.")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--limit", type=int, default=300, help="limit du snapshot univers")
    p.add_argument("--symbol", default="BTC/USDT", help="symbole pour le bench klines")
    p.add_argument("--range", dest="range_", default="1D", help="range klines (1D|7D|1M|1Y)")
    p.add_argument("--runs", type=int, default=30, help="requêtes par endpoint")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--json", action="store_true")
    ns = p.parse_args(argv)

    base = ns.base_url.rstrip("/")
    sym = urllib.parse.quote(ns.symbol, safe="")
    targets = {
        "universe_snapshot": f"{base}/api/market/universe?limit={ns.limit}",
        "klines": f"{base}/api/market/symbol/{sym}/klines?range={ns.range_}",
    }

    # Probe de connectivité (fail-fast si l'API est down).
    try:
        _time_get(f"{base}/api/health", ns.timeout)
    except urllib.error.URLError as e:
        print(f"benchmark: API injoignable sur {base} — {e}", file=sys.stderr)
        return 2

    results = {name: bench_endpoint(url, ns.runs, ns.timeout) for name, url in targets.items()}

    if ns.json:
        print(json.dumps(results, indent=2))
    else:
        print("=" * 60)
        print(f"  BENCH API d'affichage — {ns.runs} runs/endpoint @ {base}")
        print("=" * 60)
        for name, r in results.items():
            print(f"  {name}")
            print(f"    p50={r['p50']}ms  p95={r['p95']}ms  p99={r['p99']}ms  "
                  f"max={r['max']}ms  avg={r['avg']}ms")
            print(f"    n={r['n']} errors={r['errors']} payload~{r['payload_bytes_avg']}B")
        print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
