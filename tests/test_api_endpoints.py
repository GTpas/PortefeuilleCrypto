"""
Async API endpoint tests (offline).

Drives the real FastAPI app (api.main:app) over httpx's in-process ASGI
transport, with the asyncpg pool and the Binance/universe hubs replaced by
in-memory fakes. No Postgres, no network, no lifespan — so this exercises the
actual request → routing → handler → JSON path that the cockpit hits, which the
unit suite never did.

Focus: the correctness/perf changes made in this pass —
  * /api/market-features pins to settings.DISPLAY_EXCHANGE (race fix);
  * /api/watchlist and /api/signals are set-based (no per-symbol N+1);
  * /api/health pins freshness to the display exchange;
  * /api/market/universe serves hub rows (or honest 'disabled').
"""

import asyncio
import contextlib
from datetime import datetime, timezone

import pytest

pytest.importorskip("httpx")
import httpx

import api.main as apimod
from api.main import app
from config import settings


# ── Fake asyncpg pool ───────────────────────────────────────────────────────
class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Routes queries to canned results by substring-matching the SQL.

    Records every call so a test can assert the access pattern (e.g. set-based
    `fetch` instead of N per-symbol `fetchrow`) and the bound parameters.
    """

    def __init__(self, fetch_rules=(), fetchrow_rules=()):
        self._fetch_rules = list(fetch_rules)
        self._fetchrow_rules = list(fetchrow_rules)
        self.calls = []  # list of (method, query, args)

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "SELECT 1"

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        for needle, rows in self._fetch_rules:
            if needle in query:
                return rows
        return []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        for needle, row in self._fetchrow_rules:
            if needle in query:
                return row(args) if callable(row) else row
        return None


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


class _FakeUniverseHub:
    def __init__(self, rows, connected=True):
        self._rows = rows
        self.connected = connected

    def universe(self, limit=300):
        return [dict(r) for r in self._rows][:limit]

    def status(self):
        return {"connected": self.connected, "count": len(self._rows),
                "last_refresh_ms": 1234}


@contextlib.contextmanager
def _wired(pool=None, binance_hub=None, universe_hub=None):
    """Temporarily install fake globals on api.main and restore them after."""
    saved = (apimod.pool, apimod.binance_hub, apimod.universe_hub)
    apimod.pool, apimod.binance_hub, apimod.universe_hub = pool, binance_hub, universe_hub
    try:
        yield
    finally:
        apimod.pool, apimod.binance_hub, apimod.universe_hub = saved


def _get(path):
    """GET `path` against the in-process app (no lifespan, no network)."""
    async def _body():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get(path)
            return resp.status_code, resp.json()
    return asyncio.run(_body())


def _now():
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


# ── /api/market-features — the exchange race fix ────────────────────────────
def test_market_features_pins_to_display_exchange():
    """The endpoint must filter market_feature_1s by settings.DISPLAY_EXCHANGE.

    The fake returns a Binance row only when the display exchange is bound as a
    parameter; otherwise it returns a *different* exchange's row (simulating the
    pre-fix `ORDER BY ts DESC LIMIT 1` race). A correct fix always yields the
    display exchange.
    """
    def _row_for(args):
        wanted = settings.DISPLAY_EXCHANGE in args
        exch = settings.DISPLAY_EXCHANGE if wanted else "kraken"
        return {
            "ts": _now(), "symbol": "BTC/USDT", "exchange_code": exch,
            "spread_bps": 1.5, "depth_usd_10bps": 100000.0, "book_imbalance": 0.1,
            "trade_pressure": 0.2, "relative_volume": 1.1, "slippage_bps_est": 2.0,
            "bid_px": 60000.0, "ask_px": 60001.0, "mid_px": 60000.5,
        }

    conn = _FakeConn(fetchrow_rules=[("FROM market_feature_1s", _row_for)])
    with _wired(pool=_FakePool(conn)):
        status, body = _get("/api/market-features/BTC/USDT")

    assert status == 200
    assert body["exchange_code"] == settings.DISPLAY_EXCHANGE
    # Prove the bound parameter actually carried the display exchange.
    mf_calls = [c for c in conn.calls if "FROM market_feature_1s" in c[1]]
    assert mf_calls and settings.DISPLAY_EXCHANGE in mf_calls[0][2]
    assert "exchange_code" in mf_calls[0][1]


def test_market_features_unavailable_is_honest():
    conn = _FakeConn(fetchrow_rules=[("FROM market_feature_1s", None)])
    with _wired(pool=_FakePool(conn)):
        status, body = _get("/api/market-features/DOGE/USDT")
    assert status == 200
    assert body.get("error") == "No market features available"


# ── /api/watchlist — set-based, no per-symbol N+1 ───────────────────────────
def test_watchlist_is_set_based_and_sorted():
    sig_rows = [
        {"symbol": "BTC/USDT", "s_social": 0.1, "s_market": 0.2, "s_risk": 0.5,
         "s_total": 0.30, "action_proposed": "buy", "confidence_score": 0.8,
         "reason_code": "mkt_momentum", "quality_grade": "A", "social_available": True},
        {"symbol": "ETH/USDT", "s_social": 0.0, "s_market": 0.0, "s_risk": 0.5,
         "s_total": 0.70, "action_proposed": "reinforce", "confidence_score": 0.9,
         "reason_code": "strong", "quality_grade": "A", "social_available": False},
    ]
    price_rows = [
        {"symbol": "BTC/USDT", "close": 60000.0},
        {"symbol": "ETH/USDT", "close": 3000.0},
    ]
    conn = _FakeConn(fetch_rules=[
        ("FROM decision_snapshot", sig_rows),
        ("FROM ohlcv_1s", price_rows),
    ])
    with _wired(pool=_FakePool(conn)):  # binance_hub=None → DB price fallback
        status, body = _get("/api/watchlist")

    assert status == 200
    # Always returns every ACTIVE_SYMBOL (symbols with no decision get defaults),
    # sorted by s_total desc → the two scored symbols lead with ETH before BTC.
    assert len(body) == len(settings.ACTIVE_SYMBOLS)
    order = [r["symbol"] for r in body]
    assert order.index("ETH/USDT") < order.index("BTC/USDT")
    btc = next(r for r in body if r["symbol"] == "BTC/USDT")
    assert btc["price"] == 60000.0
    # Set-based: exactly two `fetch` calls, and ZERO per-symbol `fetchrow`
    # (the whole point — no N+1 fan-out regardless of symbol count).
    assert sum(1 for c in conn.calls if c[0] == "fetch") == 2
    assert sum(1 for c in conn.calls if c[0] == "fetchrow") == 0
    # Price query bound the display exchange.
    price_call = [c for c in conn.calls if "FROM ohlcv_1s" in c[1]][0]
    assert settings.DISPLAY_EXCHANGE in price_call[2]


# ── /api/signals — set-based ────────────────────────────────────────────────
def test_signals_is_set_based():
    rows = [
        {"symbol": "BTC/USDT", "s_social": 0.1, "s_market": 0.2, "s_risk": 0.5,
         "s_total": 0.3, "ts_eval": _now(), "action_proposed": "buy",
         "confidence_score": 0.8, "reason_code": "x", "quality_grade": "A",
         "social_available": True},
    ]
    conn = _FakeConn(fetch_rules=[("FROM decision_snapshot", rows)])
    with _wired(pool=_FakePool(conn)):
        status, body = _get("/api/signals")

    assert status == 200
    assert len(body) == 1 and body[0]["symbol"] == "BTC/USDT"
    assert body[0]["ts_eval"].startswith("2026-06-09")
    # One set-based fetch, no per-symbol fetchrow.
    assert sum(1 for c in conn.calls if c[0] == "fetch") == 1
    assert sum(1 for c in conn.calls if c[0] == "fetchrow") == 0


# ── /api/health — freshness pinned to the display exchange ──────────────────
def test_health_pins_freshness_to_display_exchange():
    rows = [{"symbol": settings.ACTIVE_SYMBOLS[0], "bucket_start": _now(), "age_ms": 1500.0}]
    conn = _FakeConn(fetch_rules=[("FROM ohlcv_1s", rows)])
    with _wired(pool=_FakePool(conn)):
        status, body = _get("/api/health")

    assert status == 200
    assert body["db_status"] == "up"
    assert any(s["symbol"] == settings.ACTIVE_SYMBOLS[0] for s in body["symbols"])
    freshness_call = [c for c in conn.calls if "FROM ohlcv_1s" in c[1]][0]
    assert settings.DISPLAY_EXCHANGE in freshness_call[2]


# ── /api/market/universe — serves hub rows / honest when disabled ───────────
def test_universe_serves_hub_rows_and_tags_core():
    core = settings.ACTIVE_SYMBOLS[0]
    rows = [
        {"symbol": core, "price": 60000.0, "trending_score": 9.0},
        {"symbol": "PEPE/USDT", "price": 0.00001, "trending_score": 8.0},
    ]
    hub = _FakeUniverseHub(rows, connected=True)
    with _wired(universe_hub=hub):
        status, body = _get("/api/market/universe")

    assert status == 200
    assert body["enabled"] is True and body["connected"] is True
    assert body["count"] == 2
    by_sym = {r["symbol"]: r for r in body["rows"]}
    assert by_sym[core]["is_core"] is True
    assert by_sym["PEPE/USDT"]["is_core"] is False


def test_universe_disabled_is_honest():
    with _wired(universe_hub=None):
        status, body = _get("/api/market/universe")
    assert status == 200
    assert body["enabled"] is False and body["rows"] == []


# ── /metrics — served at the conventional (no trailing slash) scrape path ───
def test_metrics_served_at_bare_path():
    """The StaticFiles mount at '/' used to shadow a bare '/metrics' (404). The
    explicit route must serve the Prometheus exposition there, incl. the new
    per-route API latency metric family."""
    async def _body():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/metrics")
            return resp.status_code, resp.headers.get("content-type", ""), resp.text
    status, ctype, text = asyncio.run(_body())
    assert status == 200
    assert "text/plain" in ctype
    assert "api_request_duration_ms" in text
