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
