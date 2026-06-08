import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import asyncpg

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from paper_execution.engine import PaperExecutionEngine

# Global DB pool
pool = None
execution_engine = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, execution_engine
    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    execution_engine = PaperExecutionEngine(pool)
    yield
    await pool.close()

app = FastAPI(lifespan=lifespan, title="Antigravity Cockpit API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus metrics exposition (shares the default registry with workers
# running in the same image; harmless if scraped standalone).
try:
    from prometheus_client import make_asgi_app
    app.mount("/metrics", make_asgi_app())
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
    """Returns all active symbols sorted by composite score (S_total desc)."""
    try:
        async with pool.acquire() as conn:
            results = []
            for symbol in settings.ACTIVE_SYMBOLS:
                # Latest signal score
                sig = await conn.fetchrow("""
                    SELECT s_social, s_market, s_risk, s_total, action_proposed,
                           confidence_score, reason_code, quality_grade
                    FROM decision_snapshot
                    WHERE symbol = $1
                    ORDER BY ts_eval DESC
                    LIMIT 1
                """, symbol)

                # Latest price
                price_row = await conn.fetchrow("""
                    SELECT close FROM ohlcv_1s
                    WHERE symbol = $1
                    ORDER BY bucket_start DESC
                    LIMIT 1
                """, symbol)

                results.append({
                    "symbol": symbol,
                    "price": float(price_row['close']) if price_row else None,
                    "s_social": float(sig['s_social']) if sig else 0.0,
                    "s_market": float(sig['s_market']) if sig else 0.0,
                    "s_risk": float(sig['s_risk']) if sig else 0.5,
                    "s_total": float(sig['s_total']) if sig else 0.0,
                    "action_proposed": sig['action_proposed'] if sig else "hold",
                    "confidence_score": float(sig['confidence_score']) if sig and sig['confidence_score'] else None,
                    "reason_code": sig['reason_code'] if sig and sig['reason_code'] else None,
                    "quality_grade": sig['quality_grade'] if sig and sig['quality_grade'] else None,
                })

            # Sort by S_total descending
            results.sort(key=lambda x: x['s_total'], reverse=True)
            return results
    except Exception as e:
        return {"error": str(e)}

# ── Signals ─────────────────────────────────

@app.get("/api/signals")
async def get_signals():
    """Returns the latest signal scores for all active symbols."""
    try:
        async with pool.acquire() as conn:
            results = []
            for symbol in settings.ACTIVE_SYMBOLS:
                sig = await conn.fetchrow("""
                    SELECT s_social, s_market, s_risk, s_total, ts_eval,
                           action_proposed, confidence_score, reason_code, quality_grade
                    FROM decision_snapshot
                    WHERE symbol = $1
                    ORDER BY ts_eval DESC
                    LIMIT 1
                """, symbol)

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
                        "reason_code": sig.get('reason_code'),
                        "quality_grade": sig.get('quality_grade'),
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
                SELECT id, ts_eval, s_social, s_market, s_risk, s_total,
                       action_proposed, confidence_score, reason_code, quality_grade
                FROM decision_snapshot
                WHERE symbol = $1
                ORDER BY ts_eval DESC
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

            # Evidence links
            evidence = await conn.fetch("""
                SELECT del.raw_content_id, del.relevance_score,
                       rc.raw_payload, rc.published_at, rc.source_url,
                       ts.name AS source_name, ta.handle AS author_handle
                FROM decision_evidence_link del
                JOIN raw_content rc ON rc.id = del.raw_content_id
                LEFT JOIN tracked_source ts ON ts.id = rc.source_id
                LEFT JOIN tracked_actor ta ON ta.id = rc.actor_id
                WHERE del.decision_snapshot_id = $1
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
    """Returns the latest market microstructure features for a symbol."""
    try:
        async with pool.acquire() as conn:
            record = await conn.fetchrow("""
                SELECT ts, symbol, exchange_code,
                       spread_bps, depth_usd_10bps, book_imbalance,
                       trade_pressure, relative_volume, slippage_bps_est,
                       bid_px, ask_px, mid_px
                FROM market_feature_1s
                WHERE symbol = $1
                ORDER BY ts DESC
                LIMIT 1
            """, symbol)

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

| S_total | Action | Signification |
|---------|--------|---------------|
| ≥ 0.80 | **Reinforce** | Renforcer la position existante |
| ≥ 0.65 | **Buy** | Ouvrir une nouvelle position |
| 0.35 – 0.65 | **Hold** | Maintenir ou observer |
| < 0.35 | **Reduce** | Réduire la position |
| < 0.15 | **Exit** | Sortir complètement |

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

# ── Historical OHLCV ───────────────────────

@app.get("/api/historical/{symbol:path}")
async def get_historical(symbol: str, limit: int = 1800):
    async with pool.acquire() as conn:
        records = await conn.fetch("""
            SELECT bucket_start, open, high, low, close, volume_base
            FROM ohlcv_1s
            WHERE symbol = $1
            ORDER BY bucket_start DESC
            LIMIT $2
        """, symbol, limit)

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
    last_candle_time = None

    try:
        while True:
            async with pool.acquire() as conn:
                record = await conn.fetchrow("""
                    SELECT bucket_start, open, high, low, close, volume_base
                    FROM ohlcv_1s
                    WHERE symbol = $1
                    ORDER BY bucket_start DESC
                    LIMIT 1
                """, symbol)

                if record:
                    candle_data = {
                        "type": "candle",
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
                    last_candle_time = record['bucket_start']

            await asyncio.sleep(1)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error for {symbol}: {e}")

# ── Static Files ────────────────────────────

frontend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
