import asyncio
import json
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import asyncpg

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from paper_execution.engine import PaperExecutionEngine
from market.binance_spot import (
    BinanceSpotHub, normalize_range, range_to_interval, CHART_RANGES,
)
from market.universe import BinanceUniverseHub

# Global DB pool
pool = None
execution_engine = None
binance_hub: BinanceSpotHub | None = None
universe_hub: BinanceUniverseHub | None = None


def _range_intervals() -> dict:
    """Range→interval map from settings (1D/7D/1M/1Y)."""
    return {
        "1D": settings.CHART_INTERVAL_1D,
        "7D": settings.CHART_INTERVAL_7D,
        "1M": settings.CHART_INTERVAL_1M,
        "1Y": settings.CHART_INTERVAL_1Y,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, execution_engine, binance_hub, universe_hub
    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    execution_engine = PaperExecutionEngine(pool)

    # Real-time Binance Spot hub feeding the cockpit's displayed price/microstructure.
    # In-process so the price matches Binance UI within network latency (the DB→
    # aggregator→ohlcv_1s path lags several seconds and mixes exchanges).
    # Tier 3 (full detail): the small ACTIVE_SYMBOLS core + the selected symbol.
    if settings.ENABLE_BINANCE_SPOT:
        # Default range governs the initial chart interval.
        initial_interval = range_to_interval(settings.CHART_RANGE_DEFAULT, _range_intervals())
        binance_hub = BinanceSpotHub(
            symbols=settings.ACTIVE_SYMBOLS,
            price_source=settings.PRICE_SOURCE,
            candle_interval=initial_interval or settings.CANDLE_INTERVAL,
            ws_base=settings.BINANCE_WS_BASE,
            rest_base=settings.BINANCE_REST_BASE,
            rest_timeout=settings.BINANCE_REST_TIMEOUT,
            rest_fallbacks=settings.BINANCE_REST_FALLBACKS,
            max_sync_retries=settings.BINANCE_REST_MAX_SYNC_RETRIES,
            depth_limit=settings.BINANCE_DEPTH_LIMIT,
            max_age_ms=settings.BINANCE_LIVE_MAX_AGE_MS,
            chart_max_age_ms=settings.CHART_LIVE_MAX_AGE_MS,
            max_candles=settings.MAX_CANDLES_BACKEND,
            active_symbol_limit=settings.BACKEND_ACTIVE_SYMBOL_LIMIT,
            range_intervals=_range_intervals(),
        )
        await binance_hub.start()

    # Tier 1 (light): top-N trending Binance Spot pairs, display-only.
    if settings.ENABLE_MARKET_UNIVERSE:
        universe_hub = BinanceUniverseHub(
            quote_asset=settings.QUOTE_ASSET,
            limit=settings.UNIVERSE_LIMIT,
            min_quote_volume=settings.MIN_QUOTE_VOLUME,
            exclude_stables=settings.EXCLUDE_STABLES,
            exclude_leverage=settings.EXCLUDE_LEVERAGE,
            refresh_seconds=settings.TRENDING_REFRESH_SECONDS,
            ws_base=settings.BINANCE_WS_BASE,
            rest_base=settings.BINANCE_REST_BASE,
            rest_timeout=settings.BINANCE_REST_TIMEOUT,
            rest_fallbacks=settings.BINANCE_REST_FALLBACKS,
            max_symbols=settings.BACKEND_MAX_SYMBOLS,
            stale_ms=settings.UNIVERSE_STALE_MS,
        )
        await universe_hub.start()

    yield

    if binance_hub:
        await binance_hub.stop()
    if universe_hub:
        await universe_hub.stop()
    await pool.close()

app = FastAPI(lifespan=lifespan, title="Antigravity Cockpit API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-route API latency instrumentation. Labels by the matched route *template*
# (e.g. /api/market-features/{symbol:path}) — never the raw URL — so cardinality
# stays bounded even with 300-symbol traffic. The histogram's _count also gives
# request totals per (method, route, status). Exposed on /metrics below.
from metrics import api_request_duration_ms


@app.middleware("http")
async def _record_request_metrics(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_label = getattr(route, "path", None) or "unmatched"
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        api_request_duration_ms.labels(
            method=request.method, route=route_label, status=str(status)
        ).observe(elapsed_ms)

# Prometheus metrics exposition (shares the default registry with workers
# running in the same image; harmless if scraped standalone). An explicit GET
# /metrics route — NOT app.mount("/metrics", …) — because the StaticFiles mount
# at "/" shadows a bare "/metrics" and would 404 the conventional scrape path
# (only "/metrics/" worked). An exact route is matched before the catch-all mount.
try:
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

    @app.get("/metrics")
    async def metrics_endpoint():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
except Exception as _e:  # pragma: no cover
    pass

# ── Symbols ─────────────────────────────────

@app.get("/api/symbols")
async def get_symbols():
    return {"symbols": settings.ACTIVE_SYMBOLS}

# ── Portfolio ───────────────────────────────

@app.get("/api/portfolio")
async def get_portfolio():
    if not execution_engine:
        return {"error": "Engine not initialized"}
    try:
        state = await execution_engine.get_portfolio_state()
        return state
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/portfolio/history")
async def get_portfolio_history(limit: int = 500):
    """Returns historical portfolio value snapshots for PnL charting."""
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT ts, total_value, current_cash, invested_value,
                       num_positions, max_position_weight, drawdown_pct, exposure_pct
                FROM portfolio_state
                ORDER BY ts DESC
                LIMIT $1
            """, limit)

            return [
                {
                    "ts": r['ts'].isoformat(),
                    "total_value": float(r['total_value']),
                    "current_cash": float(r['current_cash']),
                    "invested_value": float(r['invested_value']),
                    "num_positions": r['num_positions'],
                    "max_position_weight": float(r['max_position_weight']),
                    "drawdown_pct": float(r['drawdown_pct']),
                    "exposure_pct": float(r['exposure_pct']),
                }
                for r in reversed(records)
            ]
    except Exception as e:
        return {"error": str(e)}

# ── Watchlist (sorted by S_total) ───────────

@app.get("/api/watchlist")
async def get_watchlist():
    """Returns all active symbols sorted by composite score (S_total desc).

    Set-based: one query for the latest decision per symbol + one for the latest
    DB price (both DISTINCT ON, exchange-pinned), merged in memory — instead of a
    per-symbol N+1 fan-out (2×N round-trips). Mirrors the /api/health idiom. The
    real-time Binance hub price still takes precedence over the DB fallback.
    """
    try:
        symbols = settings.ACTIVE_SYMBOLS
        async with pool.acquire() as conn:
            # Latest decision row per symbol (+ whether social was real).
            sig_rows = await conn.fetch("""
                SELECT DISTINCT ON (ds.symbol)
                       ds.symbol, ds.s_social, ds.s_market, ds.s_risk, ds.s_total,
                       ds.action_proposed, ds.confidence_score, ds.reason_code, ds.quality_grade,
                       COALESCE(sqa.has_sufficient_social, FALSE) AS social_available
                FROM decision_snapshot ds
                LEFT JOIN signal_quality_audit sqa ON sqa.decision_snapshot_id = ds.id
                WHERE ds.symbol = ANY($1::text[])
                ORDER BY ds.symbol, ds.ts_eval DESC
            """, symbols)
            sig_by_symbol = {r['symbol']: r for r in sig_rows}

            # Latest DB price per symbol (Binance-pinned fallback).
            price_rows = await conn.fetch("""
                SELECT DISTINCT ON (symbol) symbol, close
                FROM ohlcv_1s
                WHERE symbol = ANY($1::text[]) AND exchange_code = $2
                ORDER BY symbol, bucket_start DESC
            """, symbols, settings.DISPLAY_EXCHANGE)
            price_by_symbol = {r['symbol']: float(r['close']) for r in price_rows}

        results = []
        for symbol in symbols:
            sig = sig_by_symbol.get(symbol)

            # Latest price — prefer the real-time Binance hub so the watchlist
            # matches the big displayed number; fall back to Binance-pinned OHLCV.
            price = None
            if binance_hub and binance_hub.has_symbol(symbol):
                snap = binance_hub.snapshot(symbol)
                if snap:
                    price = snap.get("displayed_price")
            if price is None:
                price = price_by_symbol.get(symbol)

            results.append({
                "symbol": symbol,
                "price": price,
                "s_social": float(sig['s_social']) if sig else 0.0,
                "s_market": float(sig['s_market']) if sig else 0.0,
                "s_risk": float(sig['s_risk']) if sig else 0.5,
                "s_total": float(sig['s_total']) if sig else 0.0,
                "action_proposed": sig['action_proposed'] if sig else "hold",
                "confidence_score": float(sig['confidence_score']) if sig and sig['confidence_score'] else None,
                "reason_code": sig['reason_code'] if sig and sig['reason_code'] else None,
                "quality_grade": sig['quality_grade'] if sig and sig['quality_grade'] else None,
                "social_available": bool(sig['social_available']) if sig else False,
            })

        # Sort by S_total descending
        results.sort(key=lambda x: x['s_total'], reverse=True)
        return results
    except Exception as e:
        return {"error": str(e)}

# ── Signals ─────────────────────────────────

@app.get("/api/signals")
async def get_signals():
    """Returns the latest signal scores for all active symbols.

    Set-based: one DISTINCT ON query for the latest decision per symbol instead
    of a per-symbol N+1 loop. Preserves ACTIVE_SYMBOLS ordering and the original
    behaviour of omitting symbols that have no decision yet.
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT ON (ds.symbol)
                       ds.symbol, ds.s_social, ds.s_market, ds.s_risk, ds.s_total, ds.ts_eval,
                       ds.action_proposed, ds.confidence_score, ds.reason_code, ds.quality_grade,
                       COALESCE(sqa.has_sufficient_social, FALSE) AS social_available
                FROM decision_snapshot ds
                LEFT JOIN signal_quality_audit sqa ON sqa.decision_snapshot_id = ds.id
                WHERE ds.symbol = ANY($1::text[])
                ORDER BY ds.symbol, ds.ts_eval DESC
            """, settings.ACTIVE_SYMBOLS)
        by_symbol = {r['symbol']: r for r in rows}

        results = []
        for symbol in settings.ACTIVE_SYMBOLS:
            sig = by_symbol.get(symbol)
            if sig:
                results.append({
                    "symbol": symbol,
                    "s_social": float(sig['s_social']),
                    "s_market": float(sig['s_market']),
                    "s_risk": float(sig['s_risk']),
                    "s_total": float(sig['s_total']),
                    "ts_eval": sig['ts_eval'].isoformat(),
                    "action_proposed": sig['action_proposed'],
                    "confidence_score": float(sig['confidence_score']) if sig['confidence_score'] else None,
                    "reason_code": sig['reason_code'],
                    "quality_grade": sig['quality_grade'],
                    "social_available": bool(sig['social_available']),
                })
        return results
    except Exception as e:
        return {"error": str(e)}

# ── Detailed Signal History ─────────────────

@app.get("/api/signals/{symbol:path}")
async def get_signal_history(symbol: str, limit: int = 50):
    """Returns the history of decisions for a specific symbol."""
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT ds.id, ds.ts_eval, ds.s_social, ds.s_market, ds.s_risk, ds.s_total,
                       ds.action_proposed, ds.confidence_score, ds.reason_code, ds.quality_grade,
                       COALESCE(sqa.has_sufficient_social, FALSE) AS social_available
                FROM decision_snapshot ds
                LEFT JOIN signal_quality_audit sqa ON sqa.decision_snapshot_id = ds.id
                WHERE ds.symbol = $1
                ORDER BY ds.ts_eval DESC
                LIMIT $2
            """, symbol, limit)

            return [
                {
                    "id": r['id'],
                    "ts_eval": r['ts_eval'].isoformat(),
                    "s_social": float(r['s_social']),
                    "s_market": float(r['s_market']),
                    "s_risk": float(r['s_risk']),
                    "s_total": float(r['s_total']),
                    "action_proposed": r['action_proposed'],
                    "confidence_score": float(r['confidence_score']) if r['confidence_score'] else None,
                    "reason_code": r.get('reason_code'),
                    "quality_grade": r.get('quality_grade'),
                    # So the timeline can gate the SOC badge like the watchlist/drilldown.
                    "social_available": bool(r['social_available']),
                }
                for r in records
            ]
    except Exception as e:
        return {"error": str(e)}

# ── Decision Drilldown ──────────────────────

@app.get("/api/decision/{decision_id}")
async def get_decision_detail(decision_id: int):
    """Returns full decision detail including factors and evidence links."""
    try:
        async with pool.acquire() as conn:
            # Snapshot
            snapshot = await conn.fetchrow("""
                SELECT id, ts_eval, symbol, exchange_code,
                       s_social, s_market, s_risk, s_total,
                       action_proposed, confidence_score, reason_code, quality_grade
                FROM decision_snapshot
                WHERE id = $1
            """, decision_id)

            if not snapshot:
                return {"error": "Decision not found"}

            # Factors
            factors = await conn.fetch("""
                SELECT factor_category, factor_name, factor_value, score_contribution, explanation
                FROM decision_factor
                WHERE decision_snapshot_id = $1
                ORDER BY abs(score_contribution) DESC
            """, decision_id)

            # Quality audit
            audit = await conn.fetchrow("""
                SELECT social_sources_count, has_sufficient_social, has_sufficient_market,
                       quality_grade, degradation_reasons
                FROM signal_quality_audit
                WHERE decision_snapshot_id = $1
                ORDER BY ts_eval DESC LIMIT 1
            """, decision_id)

            # Evidence links — REAL sources only. Mock/simulated content
            # (tracked_source.name LIKE 'mock%') is never surfaced as evidence,
            # so fabricated authors/tweets cannot reach the drilldown.
            evidence = await conn.fetch("""
                SELECT del.raw_content_id, del.relevance_score,
                       rc.raw_payload, rc.published_at, rc.source_url,
                       ts.name AS source_name, ta.handle AS author_handle
                FROM decision_evidence_link del
                JOIN raw_content rc ON rc.id = del.raw_content_id
                LEFT JOIN tracked_source ts ON ts.id = rc.source_id
                LEFT JOIN tracked_actor ta ON ta.id = rc.actor_id
                WHERE del.decision_snapshot_id = $1
                  AND COALESCE(ts.name, '') NOT ILIKE 'mock%'
                ORDER BY del.relevance_score DESC
                LIMIT 20
            """, decision_id)

            return {
                "snapshot": {
                    "id": snapshot['id'],
                    "ts_eval": snapshot['ts_eval'].isoformat(),
                    "symbol": snapshot['symbol'],
                    "s_social": float(snapshot['s_social']),
                    "s_market": float(snapshot['s_market']),
                    "s_risk": float(snapshot['s_risk']),
                    "s_total": float(snapshot['s_total']),
                    "action_proposed": snapshot['action_proposed'],
                    "confidence_score": float(snapshot['confidence_score']) if snapshot['confidence_score'] else None,
                    "reason_code": snapshot.get('reason_code'),
                    "quality_grade": snapshot.get('quality_grade'),
                },
                "factors": [
                    {
                        "category": f['factor_category'],
                        "name": f['factor_name'],
                        "value": float(f['factor_value']),
                        "contribution": float(f['score_contribution']),
                        "explanation": f['explanation'],
                    }
                    for f in factors
                ],
                # Top-level flag the frontend uses to decide whether to render the
                # social sub-score as a real number or as "n/a (unavailable)".
                "social_available": bool(audit['has_sufficient_social']) if audit else False,
                "quality_audit": {
                    "social_sources_count": audit['social_sources_count'] if audit else 0,
                    "has_sufficient_social": audit['has_sufficient_social'] if audit else False,
                    "has_sufficient_market": audit['has_sufficient_market'] if audit else False,
                    "quality_grade": audit['quality_grade'] if audit else "unknown",
                    "degradation_reasons": audit['degradation_reasons'] if audit else [],
                } if audit else None,
                "evidence": [
                    {
                        "raw_content_id": e['raw_content_id'],
                        "relevance_score": float(e['relevance_score']),
                        "source_name": e['source_name'],
                        "author_handle": e['author_handle'],
                        "source_url": e['source_url'],
                        "published_at": e['published_at'].isoformat(),
                        "text": json.loads(e['raw_payload']).get('text', '') if isinstance(e['raw_payload'], str) else e['raw_payload'].get('text', ''),
                    }
                    for e in evidence
                ],
            }
    except Exception as e:
        return {"error": str(e)}

# ── Factors ─────────────────────────────────

@app.get("/api/factors/{decision_id}")
async def get_decision_factors(decision_id: int):
    """Returns the contributing factors for a specific decision."""
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT factor_category, factor_name, factor_value, score_contribution, explanation
                FROM decision_factor
                WHERE decision_snapshot_id = $1
                ORDER BY abs(score_contribution) DESC
            """, decision_id)

            return [
                {
                    "category": r['factor_category'],
                    "name": r['factor_name'],
                    "value": float(r['factor_value']),
                    "contribution": float(r['score_contribution']),
                    "explanation": r['explanation']
                }
                for r in records
            ]
    except Exception as e:
        return {"error": str(e)}

# ── Sources per Symbol ──────────────────────

@app.get("/api/sources/{symbol:path}")
async def get_sources_for_symbol(symbol: str, limit: int = 50):
    """Returns recent social content evidence for a symbol."""
    try:
        base_asset = symbol.split('/')[0] if '/' in symbol else symbol
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT rc.id, rc.raw_payload, rc.published_at, rc.source_url,
                       ts.name AS source_name, ta.handle AS author_handle, ta.actor_type,
                       ta.influence_score,
                       ce.entity_confidence, ce.content_type
                FROM content_entity ce
                JOIN raw_content rc ON rc.id = ce.raw_content_id
                LEFT JOIN tracked_source ts ON ts.id = rc.source_id
                LEFT JOIN tracked_actor ta ON ta.id = rc.actor_id
                WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
                  AND COALESCE(ts.name, '') NOT ILIKE 'mock%'
                ORDER BY rc.published_at DESC
                LIMIT $2
            """, base_asset, limit)

            return [
                {
                    "id": r['id'],
                    "source_name": r['source_name'],
                    "author_handle": r['author_handle'],
                    "actor_type": r['actor_type'],
                    "influence_score": float(r['influence_score']) if r['influence_score'] else None,
                    "text": (json.loads(r['raw_payload']) if isinstance(r['raw_payload'], str) else r['raw_payload']).get('text', ''),
                    "published_at": r['published_at'].isoformat(),
                    "source_url": r['source_url'],
                    "entity_confidence": float(r['entity_confidence']),
                    "content_type": r['content_type'],
                }
                for r in records
            ]
    except Exception as e:
        return {"error": str(e)}

# ── Market Features ─────────────────────────

@app.get("/api/market-features/{symbol:path}")
async def get_market_features(symbol: str):
    """Returns the latest market microstructure features for a symbol.

    Pinned to settings.DISPLAY_EXCHANGE: market_feature_1s holds one row per
    exchange (PK ts,symbol,exchange_code) and the feature worker writes all of
    [binance,kraken,coinbase] each cycle, so an unfiltered `ORDER BY ts DESC
    LIMIT 1` returns whichever exchange's write landed last — a race that can
    show another venue's microstructure under a Binance-labelled cockpit.
    """
    try:
        async with pool.acquire() as conn:
            record = await conn.fetchrow("""
                SELECT ts, symbol, exchange_code,
                       spread_bps, depth_usd_10bps, book_imbalance,
                       trade_pressure, relative_volume, slippage_bps_est,
                       bid_px, ask_px, mid_px
                FROM market_feature_1s
                WHERE symbol = $1 AND exchange_code = $2
                ORDER BY ts DESC
                LIMIT 1
            """, symbol, settings.DISPLAY_EXCHANGE)

            if not record:
                return {"error": "No market features available"}

            return {
                "ts": record['ts'].isoformat(),
                "symbol": record['symbol'],
                "exchange_code": record['exchange_code'],
                "spread_bps": float(record['spread_bps']),
                "depth_usd_10bps": float(record['depth_usd_10bps']),
                "book_imbalance": float(record['book_imbalance']),
                "trade_pressure": float(record['trade_pressure']),
                "relative_volume": float(record['relative_volume']),
                "slippage_bps_est": float(record['slippage_bps_est']),
                "bid_px": float(record['bid_px']) if record['bid_px'] else None,
                "ask_px": float(record['ask_px']) if record['ask_px'] else None,
                "mid_px": float(record['mid_px']) if record['mid_px'] else None,
            }
    except Exception as e:
        return {"error": str(e)}

# ── Social Signal History ───────────────────

@app.get("/api/social-history/{symbol:path}")
async def get_social_history(symbol: str, limit: int = 100):
    """Returns historical social signal data for charting."""
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT ts_bucket, s_social, mention_velocity_z, sentiment_polarity,
                       unique_authors, engagement_velocity, bot_risk_penalty,
                       source_breakdown
                FROM social_signal_1m
                WHERE symbol = $1
                ORDER BY ts_bucket DESC
                LIMIT $2
            """, symbol, limit)

            return [
                {
                    "ts": r['ts_bucket'].isoformat(),
                    "s_social": float(r['s_social']),
                    "mention_velocity_z": float(r['mention_velocity_z']),
                    "sentiment_polarity": float(r['sentiment_polarity']),
                    "unique_authors": int(r['unique_authors']) if r['unique_authors'] else 0,
                    "engagement_velocity": float(r['engagement_velocity']) if r['engagement_velocity'] else 0,
                    "bot_risk_penalty": float(r['bot_risk_penalty']),
                    "source_breakdown": json.loads(r['source_breakdown']) if isinstance(r['source_breakdown'], str) else r['source_breakdown'],
                }
                for r in reversed(records)
            ]
    except Exception as e:
        return {"error": str(e)}

# ── System Logs ─────────────────────────────

@app.get("/api/system/logs")
async def get_system_logs(limit: int = 100):
    """Returns the backend ingestion and tracking logs."""
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT id, ts_event, component, level, message, metadata
                FROM system_log
                ORDER BY ts_event DESC
                LIMIT $1
            """, limit)

            return [
                {
                    "id": r['id'],
                    "ts_event": r['ts_event'].isoformat(),
                    "component": r['component'],
                    "level": r['level'],
                    "message": r['message'],
                    "metadata": json.loads(r['metadata']) if isinstance(r['metadata'], str) else r['metadata']
                }
                for r in records
            ]
    except Exception as e:
        return {"error": str(e)}

# ── Health / Freshness ──────────────────────

@app.get("/api/health")
async def get_health():
    """
    Real system health: DB connectivity + per-symbol market-data freshness.
    Used by the cockpit to flag STALE markets and (later) by the Ops panel.
    Never returns fabricated values — a missing series reports age = null / stale.
    """
    max_age_ms = settings.MAX_DATA_AGE_S * 1000
    db_status = "down"
    symbols = []
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
            db_status = "up"
            rows = await conn.fetch("""
                SELECT DISTINCT ON (symbol) symbol, bucket_start,
                       EXTRACT(EPOCH FROM (now() - bucket_start)) * 1000 AS age_ms
                FROM ohlcv_1s
                WHERE symbol = ANY($1::text[]) AND exchange_code = $2
                ORDER BY symbol, bucket_start DESC
            """, settings.ACTIVE_SYMBOLS, settings.DISPLAY_EXCHANGE)
            seen = {r['symbol']: r for r in rows}
            for sym in settings.ACTIVE_SYMBOLS:
                r = seen.get(sym)
                age = float(r['age_ms']) if r and r['age_ms'] is not None else None
                symbols.append({
                    "symbol": sym,
                    "last_ohlcv_age_ms": age,
                    "last_ohlcv_ts": r['bucket_start'].isoformat() if r else None,
                    "status": "no_data" if age is None else ("stale" if age > max_age_ms else "fresh"),
                })
    except Exception as e:
        return {"status": "degraded", "db_status": db_status, "error": str(e), "symbols": symbols}

    any_fresh = any(s["status"] == "fresh" for s in symbols)
    # Honest social-source state — never imply a real feed when there is none.
    social_source = "mock-only" if settings.ENABLE_MOCK_SOCIAL else "not_configured"
    # Real-time Binance Spot hub status (display path), kept distinct from the
    # DB/aggregator freshness above (persistence path).
    binance_live = binance_hub.status() if binance_hub else {"enabled": settings.ENABLE_BINANCE_SPOT, "connected": False}
    universe_status = universe_hub.status() if universe_hub else {"enabled": settings.ENABLE_MARKET_UNIVERSE, "connected": False, "count": 0}
    return {
        "status": "ok" if (db_status == "up" and any_fresh) else "degraded",
        "db_status": db_status,
        "max_data_age_ms": max_age_ms,
        "social_source": social_source,
        "binance_live": binance_live,
        "universe": universe_status,
        "symbols": symbols,
    }

# ── In-App Documentation ────────────────────

@app.get("/api/docs/signals-sentiments")
async def get_signals_docs():
    """Serves the in-app documentation for Signals & Sentiments."""
    doc_markdown = """
# Signals & Sentiments — Documentation

Le moteur **Signals & Sentiments** évalue chaque actif sur trois dimensions complémentaires pour produire un score composite **S_total** qui pilote les décisions du portefeuille paper.

---

## Les trois dimensions

### SOC — Score Social (45% du poids)
Mesure la **force narrative** autour d'un actif en analysant les contenus sociaux en quasi temps réel.

**Métriques incluses :**
- **Mention Velocity (z-score)** — vitesse de propagation des mentions vs la moyenne 24h. Un z-score > 3 = choc d'attention.
- **Sentiment Polarity** — polarité agrégée du contenu [-1 bearish, +1 bullish].
- **Unique Authors** — diversité des sources. Plus il y a d'auteurs distincts, plus le signal est fiable.
- **Engagement Velocity** — taux de croissance de l'engagement (likes, retweets, réponses).
- **Cross-Source Confirmation** — le signal vient-il de plusieurs plateformes (Twitter, Reddit, Telegram) ?
- **Novelty Score** — s'agit-il d'un nouveau récit ou de bruit récurrent ?
- **Actor Influence Score** — les auteurs sont-ils crédibles (fondateurs, protocoles officiels, chercheurs) ?
- **Bot Risk Penalty** — pénalité si une proportion élevée de posts vient de bots ou de comptes inconnus.

### MKT — Score Marché (45% du poids)
Mesure la **confirmation par le marché** du récit social.

**Métriques incluses :**
- **Returns multi-fenêtres** — 15 min, 1h, 4h. Le momentum confirme-t-il la direction ?
- **Trend Alignment** — les timeframes sont-elles alignées ? (+1 si tout est haussier, -1 si tout est baissier)
- **Book Imbalance** — pression achat vs vente dans le carnet d'ordres.
- **Trade Pressure** — flux d'exécution réel (volume buy vs sell).
- **Relative Volume** — volume actuel vs la moyenne 24h.

### RSK — Score Risque (10% du poids, mais pouvoir de veto)
Contrôle l'**exécutabilité** de la décision. Un RSK faible peut **bloquer un achat** même si SOC et MKT sont très positifs.

**Gates de non-trade :**
- Spread > 15 bps → pas d'exécution
- Slippage estimé > 40 bps → pas d'exécution
- Profondeur < $500 → pas d'exécution
- Concentration position > 20% → pas de renforcement
- Drawdown > -15% → mode défensif
- Corrélation BTC > 0.95 → risque de diversification

---

## Formule composite

```
S_total = 0.45 × SOC + 0.45 × MKT + 0.10 × (2 × RSK - 1)
```

## Seuils de décision

> `S_total ∈ [-1, +1]`, seuils **symétriques autour de 0**. Un score neutre = **HOLD**. Tout risk gate actif force **HOLD**.

| S_total | Action | Signification |
|---------|--------|---------------|
| ≥ +0.60 | **Reinforce** | Renforcer la position existante |
| ≥ +0.30 | **Buy** | Ouvrir une nouvelle position |
| −0.30 – +0.30 | **Hold** | Maintenir ou observer |
| ≤ −0.30 | **Reduce** | Réduire la position |
| ≤ −0.60 | **Exit** | Sortir complètement |

---

## Pourquoi un score élevé n'implique pas toujours un achat

Même avec SOC = +0.8 et MKT = +0.7, le moteur peut **refuser d'acheter** si :
- Le spread est trop large (le coût d'exécution mangerait le gain)
- La profondeur du carnet est insuffisante (slippage trop élevé)
- La position représenterait plus de 20% du portefeuille
- Le portefeuille est en drawdown sévère (> -15%)

C'est la hiérarchie **signal → confirmation → risque** qui évite les "fantasy fills".

---

## Sources de données

| Source | Type | Fiabilité | Rôle |
|--------|------|-----------|------|
| Twitter/X | Réseau social | Moyenne (0.6) | Impulsion, attention shock, vélocité |
| Reddit | Réseau social | Bonne (0.7) | Conviction narrative, qualité argumentative |
| Telegram | Réseau social | Moyenne (0.5) | Diffusion, canaux officiels |
| Blogs officiels | Site web | Haute (0.9) | Annonces, releases |
| Truth Social | Réseau social | Faible (0.3) | Best-effort, non critique |

---

## Limites connues

- **Bruit social** : les réseaux sociaux contiennent beaucoup de faux signaux et de manipulation
- **Latence source** : il peut y avoir un délai de quelques secondes à minutes entre un événement et sa détection
- **Biais d'échantillonnage** : certaines communautés sont plus vocales que d'autres
- **Périodes de volatilité extrême** : pendant les crashs ou les rallyes violents, les modèles de risque peuvent être saturés
- **Détection de bots** : la méthode heuristique ne remplace pas une détection ML avancée
- **Truth Social** : couverture limitée, données best-effort uniquement
"""
    return {"content": doc_markdown}

# ── Recent Trades ───────────────────────────

@app.get("/api/trades/recent")
async def get_recent_trades(limit: int = 50):
    """Returns the most recent paper trades."""
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT symbol, exchange_code, side, qty, price, slippage_bps, fees, signal_score, reason, executed_at, decision_snapshot_id
                FROM paper_trade
                ORDER BY executed_at DESC
                LIMIT $1
            """, limit)

            return [
                {
                    "symbol": r['symbol'],
                    "exchange_code": r['exchange_code'],
                    "side": r['side'],
                    "qty": float(r['qty']),
                    "price": float(r['price']),
                    "slippage_bps": float(r['slippage_bps']),
                    "fees": float(r['fees']),
                    "signal_score": float(r['signal_score']) if r['signal_score'] else None,
                    "reason": r['reason'],
                    "executed_at": r['executed_at'].isoformat(),
                    "decision_snapshot_id": r['decision_snapshot_id']
                }
                for r in records
            ]
    except Exception as e:
        return {"error": str(e)}

# ── Binance Spot live layer (real-time hub) ─

@app.get("/api/binance/config")
async def get_binance_config():
    """Live-layer config the cockpit needs to pick its price/candle source,
    chart ranges, and the memory bounds it should enforce client-side."""
    # The hub's interval changes with the selected range; report the live value.
    current_interval = binance_hub.candle_interval if binance_hub else settings.CANDLE_INTERVAL
    active_symbol = binance_hub.active_symbol if binance_hub else (
        settings.ACTIVE_SYMBOLS[0] if settings.ACTIVE_SYMBOLS else None)
    return {
        "enabled": bool(settings.ENABLE_BINANCE_SPOT and binance_hub is not None),
        "price_source": settings.PRICE_SOURCE,
        "candle_source": settings.CANDLE_SOURCE,
        "candle_interval": current_interval,
        "active_symbol": active_symbol,
        "max_age_ms": settings.BINANCE_LIVE_MAX_AGE_MS,
        "chart_live_max_age_ms": settings.CHART_LIVE_MAX_AGE_MS,
        "symbols": settings.ACTIVE_SYMBOLS,
        "connected": bool(binance_hub.connected) if binance_hub else False,
        # Chart ranges (1D/7D/1M/1Y) → interval mapping + default range.
        "chart_ranges": list(CHART_RANGES),
        "range_default": normalize_range(settings.CHART_RANGE_DEFAULT),
        "range_intervals": _range_intervals(),
        # Universe (Tier 1) availability.
        "universe_enabled": bool(settings.ENABLE_MARKET_UNIVERSE and universe_hub is not None),
        "universe_limit": settings.UNIVERSE_LIMIT,
        # Frontend memory bounds (the cockpit enforces these client-side).
        "frontend_limits": {
            "max_candles_per_symbol": settings.MAX_CANDLES_PER_SYMBOL,
            "max_visible_symbols": settings.MAX_VISIBLE_SYMBOLS,
            "max_event_buffer": settings.MAX_EVENT_BUFFER,
            "max_log_buffer": settings.MAX_LOG_BUFFER,
            "ui_update_throttle_ms": settings.UI_UPDATE_THROTTLE_MS,
            "snapshot_interval_ms": int(settings.SNAPSHOT_INTERVAL_SECONDS * 1000),
        },
    }


# ── Market universe (Tier 1: top trending, light) ───────────────────────────

@app.get("/api/market/universe")
async def get_market_universe(limit: int = 300):
    """Ranked top-trending Binance Spot pairs (light rows). Real data only —
    empty list + honest status if the universe hub is disabled / has no data."""
    if not universe_hub:
        return {"enabled": False, "connected": False, "count": 0, "rows": [],
                "reason": "universe_disabled"}
    limit = max(1, min(limit, settings.UNIVERSE_LIMIT))
    rows = universe_hub.universe(limit)
    st = universe_hub.status()
    # Tag which rows are also full-detail / bot-traded core symbols.
    core = set(settings.ACTIVE_SYMBOLS)
    for r in rows:
        r["is_core"] = r["symbol"] in core
    return {"enabled": True, "connected": st["connected"], "count": len(rows),
            "limit": limit, "last_refresh_ms": st["last_refresh_ms"],
            "source": "binance_spot", "rows": rows}


@app.get("/api/market/trending")
async def get_market_trending(limit: int = 300):
    """Alias of /api/market/universe (already ranked by trending score)."""
    return await get_market_universe(limit=limit)


@app.get("/api/market/universe/debug")
async def get_market_universe_debug():
    """Explain the final universe count: raw Binance tickers, exchangeInfo spot
    pairs, eligible count, and rejections by reason (with examples). A count below
    UNIVERSE_LIMIT is immediately attributable here (e.g. a too-high volume floor
    inflating excluded_low_volume_count)."""
    if not universe_hub:
        return {"enabled": False, "reason": "universe_disabled",
                "universe_limit": settings.UNIVERSE_LIMIT}
    return {"enabled": True, "visible_limit": settings.MAX_VISIBLE_SYMBOLS,
            **universe_hub.debug()}


@app.get("/api/market/source")
async def get_market_source():
    """What is real vs mock vs not-configured — so the cockpit never mislabels data."""
    return {
        "price": {
            "source": "binance_spot" if (binance_hub and binance_hub.connected) else "unavailable",
            "real": bool(binance_hub and binance_hub.connected),
            "price_source": settings.PRICE_SOURCE,
        },
        "chart": {
            "source": settings.CANDLE_SOURCE,
            "interval": binance_hub.candle_interval if binance_hub else settings.CANDLE_INTERVAL,
            "real": bool(binance_hub and binance_hub.connected),
        },
        "universe": {
            "source": "binance_spot" if (universe_hub and universe_hub.connected) else "unavailable",
            "real": bool(universe_hub and universe_hub.connected),
            "count": len(universe_hub.universe()) if universe_hub else 0,
        },
        "social": {
            # Honest: mock-only today, gated off by default. Never presented as real.
            "source": "mock" if settings.ENABLE_MOCK_SOCIAL else "not_configured",
            "real": False,
        },
    }


@app.get("/api/market/symbol/{symbol:path}/snapshot")
async def get_market_symbol_snapshot(symbol: str):
    """
    Full-detail snapshot if the symbol is in the Tier-3 hub; otherwise the light
    universe row. Honest 'unavailable' when neither has data.
    """
    if binance_hub and binance_hub.has_symbol(symbol):
        snap = binance_hub.snapshot(symbol)
        if snap:
            return {"tier": "full", **snap}
    if universe_hub:
        row = universe_hub.get(symbol)
        if row:
            return {"tier": "light", **row}
    return {"error": "unavailable", "symbol": symbol}


@app.get("/api/market/symbol/{symbol:path}/klines")
async def get_market_symbol_klines(symbol: str, range: str = None):
    """Real Binance Spot klines for a chart range (1D/7D/1M/1Y, incl. 1J/7J/1An)."""
    rng = normalize_range(range or settings.CHART_RANGE_DEFAULT)
    if not binance_hub:
        # Fall back to a direct REST helper so ranges still work without the hub.
        from market.binance_spot import range_to_interval as _r2i, klines_limit_for_range as _lim
        interval = _r2i(rng, _range_intervals())
        return {"symbol": symbol, "range": rng, "interval": interval, "candles": [],
                "source": "binance_kline", "reason": "hub_disabled"}
    return await binance_hub.fetch_klines_range(symbol, rng)


@app.post("/api/market/active-symbol")
async def post_active_symbol(body: dict):
    """
    Select the full-detail (Tier 3) symbol for the chart, optionally with a range.
    Returns the fresh REST klines for that range so the frontend can rebase the
    chart cleanly, plus the live config.
    """
    symbol = (body or {}).get("symbol")
    rng = normalize_range((body or {}).get("range") or settings.CHART_RANGE_DEFAULT)
    if not symbol:
        return {"ok": False, "error": "missing 'symbol'"}
    if not binance_hub:
        return {"ok": False, "error": "binance_spot hub disabled", "symbol": symbol}
    # Apply symbol + range together → a SINGLE reconnect (not two back-to-back).
    res = await binance_hub.set_active_and_range(symbol, rng)
    klines = await binance_hub.fetch_klines_range(symbol, rng)
    return {
        "ok": True,
        "symbol": symbol,
        "range": rng,
        "interval": binance_hub.candle_interval,
        "reconnect": res.get("reconnect", False),
        "tracked": res.get("tracked", []),
        "klines": klines,
    }


@app.get("/api/binance/debug/{symbol:path}")
async def get_binance_debug(symbol: str):
    """
    Raw Binance values vs the cockpit's displayed value, side by side, so a
    Binance-UI / cockpit mismatch is immediately explainable (which stream,
    which source, event time, local receive, latency, staleness).
    """
    if not binance_hub or not binance_hub.has_symbol(symbol):
        return {"error": "binance_spot hub not running for this symbol",
                "enabled": bool(settings.ENABLE_BINANCE_SPOT)}
    snap = binance_hub.snapshot(symbol)
    if snap is None:
        return {"error": "no live data"}
    return snap


@app.get("/api/binance/klines/{symbol:path}")
async def get_binance_klines(symbol: str):
    """Real Binance Spot klines for the chart (matches Binance UI at the configured interval)."""
    if not binance_hub or not binance_hub.has_symbol(symbol):
        return []
    # Report the hub's LIVE interval (changes with the selected range), not the
    # static default — otherwise range-switched candles would be mislabeled.
    return {"interval": binance_hub.candle_interval, "candles": binance_hub.klines(symbol)}


# ── Historical OHLCV ───────────────────────

@app.get("/api/historical/{symbol:path}")
async def get_historical(symbol: str, limit: int = 1800):
    # Pin to settings.DISPLAY_EXCHANGE: ohlcv_1s holds buckets for
    # binance/kraken/coinbase, and an unfiltered "latest bucket" could silently
    # return a different exchange/market (e.g. Coinbase BTC-USD ≠ Binance
    # BTC/USDT). This is a real source of the cockpit-vs-Binance gap.
    async with pool.acquire() as conn:
        records = await conn.fetch("""
            SELECT bucket_start, open, high, low, close, volume_base
            FROM ohlcv_1s
            WHERE symbol = $1 AND exchange_code = $2
            ORDER BY bucket_start DESC
            LIMIT $3
        """, symbol, settings.DISPLAY_EXCHANGE, limit)

    data = []
    for r in reversed(records):
        data.append({
            "time": int(r['bucket_start'].timestamp()),
            "open": float(r['open']),
            "high": float(r['high']),
            "low": float(r['low']),
            "close": float(r['close']),
            "value": float(r['volume_base'])
        })
    return data

# ── WebSocket Live ──────────────────────────

@app.websocket("/ws/live/{symbol:path}")
async def websocket_endpoint(websocket: WebSocket, symbol: str):
    await websocket.accept()

    # Throttle: minimum interval between snapshots pushed to this client. Bounds
    # bandwidth/CPU when many tabs/symbols are open (memory-optimization pass).
    throttle_s = max(0.1, settings.BROADCAST_THROTTLE_MS / 1000.0)

    # Preferred path: the in-process Binance Spot hub (real-time, source-pinned,
    # sub-second). If the symbol isn't yet in the hub (the user selected a symbol
    # outside the core set), promote it so it gets full-detail streams.
    if binance_hub and not binance_hub.has_symbol(symbol):
        try:
            await binance_hub.set_active_symbol(symbol)
        except Exception:
            pass

    try:
        while True:
            # Re-evaluated each tick: if this symbol is evicted from the Tier-3 hub
            # (another selection hit the limit), cleanly degrade to the DB OHLCV
            # fallback instead of streaming 'nodata' forever.
            use_hub = bool(binance_hub and binance_hub.has_symbol(symbol))
            if use_hub:
                snap = binance_hub.snapshot(symbol)
                if snap and snap.get("displayed_price") is not None:
                    payload = {
                        "type": "live",
                        # backward-compatible candle block for the chart
                        "data": snap.get("candle"),
                        "data_age_ms": snap.get("data_age_ms"),
                        "stale": snap.get("feed_status") == "stale",
                        **snap,  # symbol, source, price_source, displayed_price,
                                 # feed_status, latency_ms, event_time, raw, ticker, micro, candle
                    }
                    await websocket.send_text(json.dumps(payload))
                else:
                    # Hub running but no Binance event yet (e.g. just connected or
                    # geo-blocked). Honest "no data" — never a fabricated price.
                    await websocket.send_text(json.dumps({
                        "type": "nodata", "symbol": symbol, "source": "binance_spot",
                        "reason": "no_binance_event_yet",
                        "connected": bool(binance_hub.connected),
                    }))
                await asyncio.sleep(throttle_s)
                continue

            # ── Fallback: DB-derived OHLCV (pinned to Binance) ──
            async with pool.acquire() as conn:
                record = await conn.fetchrow("""
                    SELECT bucket_start, open, high, low, close, volume_base,
                           EXTRACT(EPOCH FROM (now() - bucket_start)) * 1000 AS age_ms
                    FROM ohlcv_1s
                    WHERE symbol = $1 AND exchange_code = $2
                    ORDER BY bucket_start DESC
                    LIMIT 1
                """, symbol, settings.DISPLAY_EXCHANGE)

                if record:
                    age_ms = float(record['age_ms']) if record['age_ms'] is not None else None
                    is_stale = age_ms is not None and age_ms > settings.MAX_DATA_AGE_S * 1000
                    candle_data = {
                        "type": "candle",
                        "source": "ohlcv_derived",
                        # Chart-feed fields mirror the hub payload so the cockpit's
                        # chart-status badge is honest on the fallback path too.
                        "chart_source": "ohlcv_derived",
                        "chart_status": "stale" if is_stale else "live",
                        "candle_age_ms": age_ms,
                        "data_age_ms": age_ms,
                        "stale": is_stale,
                        "data": {
                            "time": int(record['bucket_start'].timestamp()),
                            "open": float(record['open']),
                            "high": float(record['high']),
                            "low": float(record['low']),
                            "close": float(record['close']),
                            "value": float(record['volume_base'])
                        }
                    }
                    await websocket.send_text(json.dumps(candle_data))
                else:
                    await websocket.send_text(json.dumps({
                        "type": "nodata", "symbol": symbol, "reason": "no_ohlcv",
                    }))

            await asyncio.sleep(max(1.0, throttle_s))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error for {symbol}: {e}")

# ── Static Files ────────────────────────────

frontend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
