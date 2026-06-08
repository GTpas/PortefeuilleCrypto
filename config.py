import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    # Database Configuration
    DATABASE_URL: str = Field(
        default="postgresql://crypto_user:crypto_password@localhost:5432/crypto_market_data",
        description="The PostgreSQL connection string"
    )
    
    # Ingestion Configuration
    BATCH_MAX_ROWS: int = Field(default=1000, description="Max rows per batch before flushing to DB")
    FLUSH_EVERY_SECONDS: float = Field(default=0.25, description="Max time to wait before flushing batch to DB")
    
    # Supported Exchanges
    EXCHANGES: list[str] = Field(default=["binance", "kraken", "coinbase"])
    
    # Initial Symbols for Live Capture (Base/Quote pairs, e.g., BTC/USDT)
    ACTIVE_SYMBOLS: list[str] = Field(
        default=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        description="List of canonical symbols to subscribe to"
    )
    
    # ── Binance Spot live layer (in-process hub feeding the cockpit) ──────────
    # The cockpit's *displayed* price comes from this real-time hub, NOT from the
    # DB→aggregator→ohlcv_1s path (which lags several seconds and mixes exchanges).
    # The hub is Binance SPOT only by default; it never touches futures unless a
    # caller explicitly overrides BINANCE_WS_BASE / BINANCE_REST_BASE.
    ENABLE_BINANCE_SPOT: bool = Field(default=True, description="Run the in-process Binance Spot live hub (real-time price/microstructure for the cockpit)")
    # Which raw Binance value becomes the cockpit's displayed price:
    #   trade        → last raw trade price  (p of <symbol>@trade)        [freshest]
    #   aggTrade     → last aggregated trade price (p of <symbol>@aggTrade)
    #   ticker_last  → c of <symbol>@ticker (aligns with Binance 24h header)
    #   book_mid     → (best_bid + best_ask) / 2 from <symbol>@bookTicker
    #   kline_close  → close of the in-progress <symbol>@kline_<interval>
    PRICE_SOURCE: str = Field(default="trade", description="Source of the displayed price: trade|aggTrade|ticker_last|book_mid|kline_close")
    # Chart candles: real Binance klines (matches Binance UI) vs derived 1s OHLCV.
    CANDLE_SOURCE: str = Field(default="binance_kline", description="Chart candle source: binance_kline|derived_trades")
    CANDLE_INTERVAL: str = Field(default="1m", description="Binance kline interval for the chart: 1s|1m|5m|15m|1h|4h|1d")
    BINANCE_WS_BASE: str = Field(default="wss://stream.binance.com:9443", description="Binance Spot combined-stream WS base (NOT fstream/futures)")
    BINANCE_REST_BASE: str = Field(default="https://api.binance.com", description="Binance Spot REST base for klines/depth/ticker snapshots")
    BINANCE_DEPTH_LIMIT: int = Field(default=100, description="REST order-book snapshot depth (5|10|20|50|100|500|1000)")
    BINANCE_LIVE_MAX_AGE_MS: int = Field(default=3000, description="Displayed price counts as LIVE only if a real Binance event arrived within this window (ms); older → STALE")
    # Chart counts as CHART LIVE only if a kline arrived within this window. Klines
    # push ~every 2s for >=1m intervals (1s for the 1s interval), so this is looser
    # than the price freshness window above. Older → CHART STALE; never received → NO CANDLES.
    CHART_LIVE_MAX_AGE_MS: int = Field(default=6000, description="Chart (kline) freshness window (ms): CHART LIVE if a kline arrived within it, else CHART STALE")

    # Feature Flags
    ENABLE_L2_BOOK: bool = Field(default=False, description="Whether to ingest level 2 full order book (Warning: high volume)")
    ENABLE_COINGECKO: bool = Field(default=False, description="Whether to run CoinGecko enrichment worker")
    ENABLE_DEX: bool = Field(default=False, description="Whether to enable DEX (Uniswap) ingestion")
    # Social data is MOCK ONLY today. Disabled by default so the cockpit never
    # presents fabricated tweets/authors/scores as real. Set to True ONLY for
    # local development of the social pipeline — content is always tagged mock
    # and the API filters it out of evidence/scores regardless.
    ENABLE_MOCK_SOCIAL: bool = Field(default=False, description="Run the simulated social collector (DEV ONLY — never present its output as real data)")

    # Redis
    REDIS_URL: str = Field(default="redis://localhost:6379/0", description="Redis connection URL for queue/cache")

    # Decision safety
    MAX_DATA_AGE_S: int = Field(default=30, description="Max age (seconds) of latest market quote before the risk engine blocks trading (data_stale gate)")

    # Ops supervisor (local dev process manager + Ops/Terminals panel)
    OPS_HOST: str = Field(default="127.0.0.1", description="Bind host for the Ops supervisor HTTP/WS server")
    OPS_PORT: int = Field(default=8050, description="Port for the Ops supervisor API (/api/ops/*, /ws/ops)")
    OPS_MAX_RESTARTS: int = Field(default=5, description="Max auto-restarts of a process within OPS_RESTART_WINDOW_S before it is marked degraded")
    OPS_RESTART_WINDOW_S: int = Field(default=120, description="Sliding window (s) for the restart budget")

    # Observability — Prometheus metrics HTTP ports (one per worker process)
    METRICS_ENABLED: bool = Field(default=True, description="Expose Prometheus metrics from workers")
    METRICS_PORT_INGESTOR: int = Field(default=9101)
    METRICS_PORT_FEATURE: int = Field(default=9102)
    METRICS_PORT_SOCIAL: int = Field(default=9103)
    METRICS_PORT_BOT: int = Field(default=9104)

    # Logging
    LOG_LEVEL: str = Field(default="INFO")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
