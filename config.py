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

    # Logging
    LOG_LEVEL: str = Field(default="INFO")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
