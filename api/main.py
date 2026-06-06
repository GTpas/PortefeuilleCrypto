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
                    FROM signal_log
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
                    FROM signal_log
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

# ── Recent Trades ───────────────────────────

@app.get("/api/trades/recent")
async def get_recent_trades(limit: int = 50):
    """Returns the most recent paper trades."""
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch("""
                SELECT symbol, exchange_code, side, qty, price, slippage_bps, fees, signal_score, reason, executed_at
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
