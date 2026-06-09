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

    # Single source of truth for which exchange the cockpit's DB-backed "latest"
    # reads (price/microstructure/freshness) are pinned to. market_feature_1s and
    # ohlcv_1s hold one row per exchange for the same symbol; an unfiltered
    # "latest" read races across them and can show e.g. Coinbase BTC-USD under a
    # Binance BTC/USDT label. Every display query filters on this constant.
    DISPLAY_EXCHANGE: str = Field(default="binance", description="Exchange the cockpit's DB-backed latest price/feature/freshness reads are pinned to")

    # Feature worker: bound the per-cycle concurrency of compute_features. The
    # worker is intentionally scoped to the small ACTIVE_SYMBOLS core (not the
    # 300-symbol display universe), so the default is modest; raise it only if
    # ACTIVE_SYMBOLS is grown toward universe size.
    FEATURE_MAX_CONCURRENCY: int = Field(default=8, description="Max concurrent compute_features() calls per feature-worker cycle")

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
    BINANCE_REST_TIMEOUT: float = Field(default=6.0, description="Timeout (s) per Binance REST API call (klines/depth/ticker). Shorter = fail-fast when the primary is blocked")
    BINANCE_REST_FALLBACKS: list[str] = Field(default=["https://api1.binance.com", "https://api2.binance.com"], description="Fallback Binance REST base URLs tried in order when the primary times out")
    BINANCE_REST_MAX_SYNC_RETRIES: int = Field(default=3, description="Max order-book depth resync retries before giving up (resets on next WS reconnect)")
    BINANCE_DEPTH_LIMIT: int = Field(default=100, description="REST order-book snapshot depth (5|10|20|50|100|500|1000)")
    BINANCE_LIVE_MAX_AGE_MS: int = Field(default=3000, description="Displayed price counts as LIVE only if a real Binance event arrived within this window (ms); older → STALE")
    # Chart counts as CHART LIVE only if a kline arrived within this window. Klines
    # push ~every 2s for >=1m intervals (1s for the 1s interval), so this is looser
    # than the price freshness window above. Older → CHART STALE; never received → NO CANDLES.
    CHART_LIVE_MAX_AGE_MS: int = Field(default=6000, description="Chart (kline) freshness window (ms): CHART LIVE if a kline arrived within it, else CHART STALE")

    # ── Market universe (Tier 1: ≤300 trending symbols, light) ────────────────
    # Display-only light tier. ONE Binance Spot all-market !ticker@arr stream feeds
    # an in-memory, bounded ranking — it never opens 300×(trade/kline/depth) streams.
    # Separate from ACTIVE_SYMBOLS (the small bot-traded / persisted core).
    ENABLE_MARKET_UNIVERSE: bool = Field(default=True, description="Run the in-process light universe hub (top trending Binance Spot pairs, display-only)")
    UNIVERSE_LIMIT: int = Field(default=300, description="Max number of trending symbols kept in the universe (hard cap)")
    QUOTE_ASSET: str = Field(default="USDT", description="Quote asset for the universe (e.g. USDT). Only <BASE>/<QUOTE> spot pairs are considered")
    # Liquidity floor to enter the universe. Sized so ~300+ Binance USDT spot pairs
    # qualify (a 5M floor only leaves ~70 → the cockpit capped at ~66). Lowering it
    # to 500K leaves ~305 eligible, so the top-N=300 ranking fills. Raise it to
    # tighten liquidity, but the universe count will shrink accordingly (see
    # /api/market/universe/debug.excluded_low_volume_count).
    MIN_QUOTE_VOLUME: float = Field(default=500_000.0, description="Minimum 24h quote volume (quote asset) for a pair to enter the universe (liquidity floor)")
    EXCLUDE_STABLES: bool = Field(default=True, description="Exclude pure stablecoin/fiat bases (USDC, FDUSD, EUR…) from the universe")
    EXCLUDE_LEVERAGE: bool = Field(default=True, description="Exclude leverage tokens (UP/DOWN/BULL/BEAR, 3L/3S…) from the universe")
    TRENDING_REFRESH_SECONDS: int = Field(default=60, description="How often the universe ranking is recomputed from the live ticker state")
    UNIVERSE_STALE_MS: int = Field(default=15000, description="A universe row is flagged stale if its last ticker is older than this (ms)")

    # ── Backend memory bounds (Tier separation: light universe vs heavy selected) ──
    BACKEND_MAX_SYMBOLS: int = Field(default=300, description="Hard cap on symbols retained in the universe state (memory bound)")
    BACKEND_ACTIVE_SYMBOL_LIMIT: int = Field(default=20, description="Max symbols allowed in the full-detail (Tier 3) Binance Spot hub at once")
    MAX_CANDLES_BACKEND: int = Field(default=1500, description="Max klines retained per symbol in the Binance Spot hub cache")
    MAX_MARKET_EVENTS: int = Field(default=50000, description="Bounded size of the ingestor's in-memory trade/bbo queues (events dropped with a warning when full — back-pressure bound)")
    BROADCAST_THROTTLE_MS: int = Field(default=500, description="Minimum interval between /ws/live snapshots pushed to a client (ms)")
    SNAPSHOT_INTERVAL_SECONDS: float = Field(default=3.0, description="Interval between universe snapshots served to the cockpit (light tier)")
    ENABLE_DEPTH_ONLY_FOR_SELECTED: bool = Field(default=True, description="Only maintain the full L2 order book for full-detail (Tier 3) symbols, never the universe")

    # ── Chart ranges (1D / 7D / 1M / 1Y) → Binance kline interval mapping ──────
    CHART_RANGE_DEFAULT: str = Field(default="1D", description="Default chart range on load: 1D|7D|1M|1Y")
    CHART_INTERVAL_1D: str = Field(default="1m", description="Kline interval for the 1D (1J) range")
    CHART_INTERVAL_7D: str = Field(default="15m", description="Kline interval for the 7D (7J) range")
    CHART_INTERVAL_1M: str = Field(default="1h", description="Kline interval for the 1M (1 month) range")
    CHART_INTERVAL_1Y: str = Field(default="1d", description="Kline interval for the 1Y (1 year) range")

    # ── Frontend memory bounds (served to the cockpit via /api/binance/config) ──
    MAX_CANDLES_PER_SYMBOL: int = Field(default=1500, description="Max candles the chart keeps per symbol (older ones trimmed)")
    MAX_VISIBLE_SYMBOLS: int = Field(default=60, description="Max watchlist rows rendered to the DOM at once (windowed/virtualized list)")
    MAX_EVENT_BUFFER: int = Field(default=200, description="Max activity/decision feed items kept in the frontend ring buffer")
    MAX_LOG_BUFFER: int = Field(default=600, description="Max Ops log lines kept in the frontend ring buffer")
    UI_UPDATE_THROTTLE_MS: int = Field(default=400, description="Min interval between heavy frontend re-renders (throttle)")

    # Feature Flags
    ENABLE_L2_BOOK: bool = Field(default=False, description="Whether to ingest level 2 full order book (Warning: high volume)")
    ENABLE_DEX: bool = Field(default=False, description="Whether to enable DEX (Uniswap) ingestion")

    # ── Global market context (macro tier: total mcap / dominance / DeFi TVL / sentiment) ──
    # An in-process, display-only hub (like the Binance hubs) that polls a few FREE,
    # no-API-key, ToS-safe public endpoints to give the cockpit the macro backdrop the
    # Binance-only tiers lack. Real data only — a source that never answers shows n/a,
    # never a fabricated number. Tiny, bounded memory; never feeds the bot/persistence.
    ENABLE_GLOBAL_CONTEXT: bool = Field(default=True, description="Run the in-process global market-context hub (macro: total mcap, dominance, DeFi TVL, Fear & Greed)")
    # Per-source sub-toggles (master flag above must also be on).
    ENABLE_COINGECKO: bool = Field(default=True, description="Global-context source: CoinGecko /global (total market cap, 24h volume, BTC/ETH dominance)")
    ENABLE_DEFILLAMA: bool = Field(default=True, description="Global-context source: DefiLlama /v2/chains (total DeFi TVL)")
    ENABLE_FEAR_GREED: bool = Field(default=True, description="Global-context source: alternative.me Fear & Greed sentiment index")
    GLOBAL_CONTEXT_REFRESH_SECONDS: int = Field(default=60, description="How often the global-context hub re-polls its macro sources (s)")
    GLOBAL_CONTEXT_HTTP_TIMEOUT: float = Field(default=10.0, description="Per-call HTTP timeout for global-context sources (s)")
    GLOBAL_CONTEXT_STALE_MS: int = Field(default=300_000, description="A global-context source value is flagged stale if older than this (ms)")
    COINGECKO_API_BASE: str = Field(default="https://api.coingecko.com/api/v3", description="CoinGecko REST base (public free tier by default)")
    COINGECKO_API_KEY: str = Field(default="", description="Optional CoinGecko Demo API key (x-cg-demo-api-key). Empty = free public tier")
    DEFILLAMA_API_BASE: str = Field(default="https://api.llama.fi", description="DefiLlama REST base (free, no key)")
    FEAR_GREED_API_BASE: str = Field(default="https://api.alternative.me", description="Fear & Greed (alternative.me) REST base (free, no key)")

    # ── DeFi protocol tier (ranked list: top protocols by TVL — DefiLlama /protocols) ──
    # Display-only ranked-list hub (like the Binance universe), on top of the macro
    # DeFi-TVL aggregate above. Real data only; CEX/Chain rows excluded so the panel
    # shows genuine DeFi protocols, not exchange reserves. Reuses DEFILLAMA_API_BASE.
    ENABLE_DEFI_PROTOCOLS: bool = Field(default=True, description="Run the in-process DeFi-protocol hub (top protocols by TVL, display-only)")
    DEFI_PROTOCOLS_LIMIT: int = Field(default=50, description="Max DeFi protocols kept in the ranked list (hard cap / memory bound)")
    DEFI_PROTOCOLS_MIN_TVL: float = Field(default=1_000_000.0, description="Minimum TVL (USD) for a protocol to enter the ranking (noise floor)")
    DEFI_PROTOCOLS_REFRESH_SECONDS: int = Field(default=120, description="How often the DeFi-protocol hub re-polls DefiLlama /protocols (s)")
    DEFI_PROTOCOLS_HTTP_TIMEOUT: float = Field(default=15.0, description="HTTP timeout (s) for DefiLlama /protocols (heavier ~7.6k-entry response → looser than the macro timeout)")
    DEFI_PROTOCOLS_STALE_MS: int = Field(default=600_000, description="A DeFi-protocol snapshot is flagged stale if older than this (ms)")
    DEFI_EXCLUDE_CATEGORIES: list[str] = Field(default=["CEX", "Chain"], description="DefiLlama categories excluded from the DeFi ranking (CEX reserves, chain-level rows)")
    # Social data is MOCK ONLY today. Disabled by default so the cockpit never
    # presents fabricated tweets/authors/scores as real. Set to True ONLY for
    # local development of the social pipeline — content is always tagged mock
    # and the API filters it out of evidence/scores regardless.
    ENABLE_MOCK_SOCIAL: bool = Field(default=False, description="Run the simulated social collector (DEV ONLY — never present its output as real data)")

    # ── Real social source: public crypto-news RSS feeds (ToS-safe) ───────────
    # The first REAL social/news connector behind BaseSocialCollector. RSS feeds
    # are published for syndication, so polling them is ToS-safe (unlike scraping
    # X/Reddit). Off by default; turn on to feed genuine (non-mock) content into
    # the social pipeline. Polled at most every RSS_POLL_SECONDS to stay polite.
    ENABLE_RSS_SOCIAL: bool = Field(default=False, description="Run the real RSS news collector (public crypto-news feeds, ToS-safe). Output is REAL, never tagged mock")
    RSS_FEEDS: list[str] = Field(
        default=[
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://cointelegraph.com/rss",
            "https://decrypt.co/feed",
            "https://www.theblock.co/rss.xml",
        ],
        description="Public RSS/Atom feed URLs for the real news collector",
    )
    RSS_POLL_SECONDS: int = Field(default=120, description="Minimum interval (s) between RSS feed fetches (politeness / ToS)")
    RSS_HTTP_TIMEOUT: float = Field(default=10.0, description="Per-feed HTTP fetch timeout (s)")

    # ── Ex-post outcome evaluation (backtest + actor credibility) ─────────────
    # The outcome_evaluator worker scores past decisions against realized price
    # moves (outcome_eval) and re-derives actor credibility (source_influence_
    # snapshot + tracked_actor.influence_score). Read-mostly; safe to leave on.
    ENABLE_OUTCOME_EVAL: bool = Field(default=True, description="Run the ex-post outcome evaluator (fills outcome_eval + source_influence_snapshot)")
    OUTCOME_EVAL_INTERVAL_S: int = Field(default=60, description="How often the outcome evaluator scans for matured decisions (s)")
    OUTCOME_HORIZONS: list[str] = Field(default=["1h", "4h", "24h"], description="Evaluation horizons after each decision (subset of 15m|1h|4h|24h|3d)")
    OUTCOME_HOLD_BAND_PCT: float = Field(default=0.5, description="A HOLD decision counts as correct if |return| over the horizon stays within this band (%)")
    OUTCOME_PRICE_TOLERANCE_S: int = Field(default=300, description="Max gap (s) between the horizon target time and the nearest OHLCV close used as the horizon price")

    # ── Daily Crypto Intelligence Report (advisory tier — display/report-only) ──
    # A scheduled worker generates, once per day, a beginner-readable yet credible
    # advisory report over the ~300-symbol universe: global ranking, prudent
    # indicative predictions, explainable BUY/HOLD/SELL/AVOID signals, transparent
    # ratios and an A+→E rating. Real data only (Binance 24h ticker + macro tier);
    # unavailable inputs (1h/7d/30d change, market cap) are shown as N/A, never
    # fabricated, and predictions are always framed as probabilities/scenarios.
    # It is display/report-only — it never feeds the bot or the persistence path.
    ENABLE_DAILY_REPORT: bool = Field(default=True, description="Run the scheduled daily-report worker (advisory report over the universe)")
    DAILY_REPORT_HOUR: int = Field(default=0, description="Local hour (0-23) at which the daily report is generated")
    DAILY_REPORT_MINUTE: int = Field(default=0, description="Local minute (0-59) at which the daily report is generated")
    DAILY_REPORT_TIMEZONE: str = Field(default="UTC", description="IANA timezone for the generation schedule (e.g. UTC, Europe/Paris). Falls back to UTC if unavailable")
    DAILY_REPORT_DIR: str = Field(default="reports", description="Directory (relative to repo root or absolute) where report JSON/Markdown artifacts are written")
    DAILY_REPORT_UNIVERSE_LIMIT: int = Field(default=300, description="Max number of universe symbols included in the daily report")
    DAILY_REPORT_TOP_N: int = Field(default=10, description="Length of the executive-summary top lists (top buy/sell/watchlist)")
    DAILY_REPORT_HISTORY_LIMIT: int = Field(default=90, description="Max number of past reports returned by the history endpoint")
    DAILY_REPORT_API_BASE: str = Field(default="http://127.0.0.1:8000", description="Base URL the report worker calls to read the live universe/macro tiers")
    DAILY_REPORT_HTTP_TIMEOUT: float = Field(default=20.0, description="HTTP timeout (s) for the report worker's reads from the local API")
    DAILY_REPORT_PERSIST_DB: bool = Field(default=True, description="Best-effort mirror the report index (+ per-asset scores) into Postgres (files remain the source of truth)")

    # Redis
    REDIS_URL: str = Field(default="redis://localhost:6379/0", description="Redis connection URL for queue/cache")

    # Decision safety
    MAX_DATA_AGE_S: int = Field(default=30, description="Max age (seconds) of latest market quote before the risk engine blocks trading (data_stale gate)")

    # Decision drill-down "Source Evidence" freshness thresholds (ms). An evidence
    # group is `available` if its data age < AVAILABLE_MS, `stale` if < STALE_MS,
    # else `unavailable`. Display-only; never gates trading.
    SOURCE_EVIDENCE_AVAILABLE_MS: int = Field(default=5000, description="Source-evidence group counts as fresh/available below this data age (ms)")
    SOURCE_EVIDENCE_STALE_MS: int = Field(default=60000, description="Source-evidence group counts as stale below this data age (ms); older/null → unavailable")

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
    METRICS_PORT_AGGREGATOR: int = Field(default=9105)
    METRICS_PORT_OUTCOME: int = Field(default=9106)
    METRICS_PORT_REPORT: int = Field(default=9107)

    # Logging
    LOG_LEVEL: str = Field(default="INFO")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
