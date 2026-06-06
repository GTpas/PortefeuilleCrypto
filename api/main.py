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

# Global DB pool
pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    yield
    await pool.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/symbols")
async def get_symbols():
    return {"symbols": settings.ACTIVE_SYMBOLS}

@app.get("/api/historical/{symbol:path}")
async def get_historical(symbol: str, limit: int = 1800):
    # Fetch last 30 minutes of 1s candles (1800 candles)
    async with pool.acquire() as conn:
        records = await conn.fetch("""
            SELECT bucket_start, open, high, low, close, volume_base
            FROM ohlcv_1s
            WHERE symbol = $1
            ORDER BY bucket_start DESC
            LIMIT $2
        """, symbol, limit)
    
    # Sort chronologically for the frontend chart
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

@app.websocket("/ws/live/{symbol:path}")
async def websocket_endpoint(websocket: WebSocket, symbol: str):
    await websocket.accept()
    
    # Start polling the DB for the latest candle and trades
    # In a production app with TimescaleDB we could use logical replication or NOTIFY/LISTEN, 
    # but since Timescale continuous aggregates don't support LISTEN natively easily on insert,
    # polling every 1 second is fine for a simple dashboard.
    last_candle_time = None
    
    try:
        while True:
            async with pool.acquire() as conn:
                # Fetch the most recent candle
                record = await conn.fetchrow("""
                    SELECT bucket_start, open, high, low, close, volume_base
                    FROM ohlcv_1s
                    WHERE symbol = $1
                    ORDER BY bucket_start DESC
                    LIMIT 1
                """, symbol)
                
                if record:
                    current_candle_time = record['bucket_start']
                    # Send update to frontend
                    candle_data = {
                        "type": "candle",
                        "data": {
                            "time": int(current_candle_time.timestamp()),
                            "open": float(record['open']),
                            "high": float(record['high']),
                            "low": float(record['low']),
                            "close": float(record['close']),
                            "value": float(record['volume_base'])
                        }
                    }
                    await websocket.send_text(json.dumps(candle_data))
                    last_candle_time = current_candle_time
            
            # Wait 1 second before next poll
            await asyncio.sleep(1)
            
    except WebSocketDisconnect:
        print(f"Client disconnected for symbol {symbol}")
    except Exception as e:
        print(f"WebSocket error: {e}")

# Mount static files for the frontend
frontend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
