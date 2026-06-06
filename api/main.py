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
                    SELECT s_social, s_market, s_risk, s_total
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
                    SELECT s_social, s_market, s_risk, s_total, ts_eval
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
                    })
            return results
    except Exception as e:
        return {"error": str(e)}

# ── Detailed Explanations ───────────────────

@app.get("/api/signals/{symbol:path}")
async def get_signal_history(symbol: str, limit: int = 50):
    """Returns the history of decisions for a specific symbol."""
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT id, ts_eval, s_social, s_market, s_risk, s_total, action_proposed, confidence_score
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
                    "confidence_score": float(r['confidence_score']) if r['confidence_score'] else None
                }
                for r in records
            ]
    except Exception as e:
        return {"error": str(e)}

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

@app.get("/api/docs/signals-sentiments")
async def get_signals_docs():
    """Serves the in-app documentation for Signals & Sentiments."""
    doc_markdown = """
# Signals & Sentiments Documentation

The **Signals & Sentiments** engine evaluates assets based on three core dimensions: **Social (SOC)**, **Market (MKT)**, and **Risk (RSK)**.

## How Scores are Calculated

- **SOC (Social)**: Measures the narrative strength. It includes Mention Velocity (how fast people are talking), Bot Risk Penalty (discounting fake engagement), and Sentiment Polarity.
- **MKT (Market)**: Measures market confirmation. It looks at 15m/1h momentum, relative volume, and order book pressure.
- **RSK (Risk)**: Measures execution safety. A low RSK score means high spread, low depth, or high volatility.
- **Σ (S_total)**: The composite score used for decision making.
  - Formula: `S_total = 0.45 * SOC + 0.45 * MKT + 0.10 * (2 * RSK - 1)`

## Understanding Decisions

A high `S_total` (e.g., > 0.65) does not guarantee a buy if risk gates are triggered. The engine logs every contributing factor, allowing full traceability of why a trade was executed, rejected, or exited.
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
