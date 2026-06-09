"""
Prometheus metrics
------------------
Central registry of metrics exposed by the PortefeuilleCrypto workers and API.
Each long-running worker process calls ``start_metrics_server(port)`` once at
startup; the FastAPI app mounts the same default registry at ``/metrics``.

Importing this module has no side effects beyond defining the metric objects,
so it is safe to import everywhere. Instrumentation is best-effort: if
``prometheus_client`` is missing or metrics are disabled, helpers degrade to
no-ops instead of breaking the hot path.
"""

import logging

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    _PROM_AVAILABLE = True
except Exception:  # pragma: no cover - prometheus_client should be installed
    _PROM_AVAILABLE = False

    # Minimal no-op fallbacks so instrumented code never crashes.
    class _Noop:
        def __init__(self, *args, **kwargs):
            pass

        def labels(self, *args, **kwargs):
            return self

        def inc(self, *args, **kwargs):
            pass

        def dec(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            pass

    Counter = Gauge = Histogram = _Noop  # type: ignore

    def start_http_server(*args, **kwargs):  # type: ignore
        logger.warning("prometheus_client unavailable; metrics server not started")


# ── Market ingestion ─────────────────────────
market_ws_connected = Gauge(
    "market_ws_connected", "WebSocket connection state per exchange (1=up, 0=down)", ["exchange"]
)
market_ws_reconnect_total = Counter(
    "market_ws_reconnect_total", "Total WebSocket reconnections", ["exchange"]
)
market_events_total = Counter(
    "market_events_total", "Total normalized market events received", ["exchange", "kind"]
)
market_ingest_lag_ms = Histogram(
    "market_ingest_lag_ms", "Lag between event source time and ingestion (ms)",
    buckets=(5, 25, 50, 100, 250, 500, 1000, 5000),
)

# ── Binance Spot live hub (in-process, cockpit display) ──────────
binance_live_connected = Gauge(
    "binance_live_connected", "Binance Spot live-hub WS connection state (1=up, 0=down)"
)
binance_live_events_total = Counter(
    "binance_live_events_total", "Raw Binance Spot stream events processed by the live hub", ["stream"]
)
binance_live_staleness_ms = Gauge(
    "binance_live_staleness_ms", "Age (ms) of the displayed price per symbol in the live hub", ["symbol"]
)
binance_live_latency_ms = Histogram(
    "binance_live_latency_ms", "Binance event-time → local-receive latency (ms)",
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 5000),
)
binance_book_resync_total = Counter(
    "binance_book_resync_total", "Order-book resyncs (gap detected in depth update IDs)", ["symbol"]
)

# ── Market universe (Tier 1: light, top-trending ranking) ──────────
universe_refresh_total = Counter(
    "universe_refresh_total", "Universe REST snapshot rebuilds attempted"
)
universe_refresh_errors_total = Counter(
    "universe_refresh_errors_total", "Universe REST snapshot rebuilds that failed (kept previous snapshot)"
)
universe_refresh_latency_ms = Histogram(
    "universe_refresh_latency_ms", "Time to build one universe snapshot (ms)",
    buckets=(25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
)
universe_symbols_loaded = Gauge(
    "universe_symbols_loaded", "Symbols in the current ranked universe snapshot"
)
universe_symbols_eligible = Gauge(
    "universe_symbols_eligible", "Symbols passing all filters before the top-N cap"
)
universe_cache_age_ms = Gauge(
    "universe_cache_age_ms", "Age (ms) of the current universe snapshot"
)

# ── Global market context (macro tier: mcap / dominance / DeFi TVL / sentiment) ──
global_context_refresh_total = Counter(
    "global_context_refresh_total", "Global-context source refreshes attempted", ["source"]
)
global_context_refresh_errors_total = Counter(
    "global_context_refresh_errors_total", "Global-context refreshes that failed (kept last value)", ["source"]
)
global_context_refresh_latency_ms = Histogram(
    "global_context_refresh_latency_ms", "Time to fetch+parse one global-context source (ms)",
    ["source"], buckets=(25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
)
global_total_market_cap_usd = Gauge(
    "global_total_market_cap_usd", "Total crypto market capitalization (USD, CoinGecko)"
)
global_btc_dominance_pct = Gauge(
    "global_btc_dominance_pct", "Bitcoin dominance (% of total market cap, CoinGecko)"
)
global_defi_tvl_usd = Gauge(
    "global_defi_tvl_usd", "Total DeFi TVL across chains (USD, DefiLlama)"
)
global_fear_greed_index = Gauge(
    "global_fear_greed_index", "Crypto Fear & Greed index [0,100] (alternative.me)"
)

# ── Aggregator (trade_tick → ohlcv_1s) ──────────
aggregator_cycles_total = Counter(
    "aggregator_cycles_total", "Aggregation cycles run (loop iterations)"
)
aggregator_rows_upserted_total = Counter(
    "aggregator_rows_upserted_total", "OHLCV 1s rows upserted by the aggregator"
)
aggregator_lag_ms = Gauge(
    "aggregator_lag_ms", "Age (ms) of the newest trade_tick at aggregation time (source freshness/lag)"
)
aggregator_cycle_latency_ms = Histogram(
    "aggregator_cycle_latency_ms", "Time to run one aggregation cycle (ms)",
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 5000),
)

# ── Outcome evaluation (ex-post decision quality) ──────────
outcome_evals_written_total = Counter(
    "outcome_evals_written_total", "Outcome evaluations written", ["horizon"]
)
outcome_eval_accuracy = Gauge(
    "outcome_eval_accuracy", "Rolling share of correct decisions per horizon [0,1]", ["horizon"]
)
actor_influence_updates_total = Counter(
    "actor_influence_updates_total", "tracked_actor.influence_score recomputations written"
)

# ── Storage / writer ─────────────────────────
db_write_latency_ms = Histogram(
    "db_write_latency_ms", "Batch DB write latency (ms)",
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 5000),
)
rows_written_total = Counter("rows_written_total", "Rows written to DB", ["table"])
dlq_total = Counter("dlq_total", "Events written to the dead-letter queue", ["channel"])
queue_depth = Gauge("queue_depth", "Current in-memory queue depth", ["queue"])

# ── Social / news ────────────────────────────
social_posts_collected_total = Counter(
    "social_posts_collected_total", "Social/news items ingested", ["source"]
)
content_analyzed_total = Counter("content_analyzed_total", "Content items analyzed for entities")
symbols_detected_total = Counter("symbols_detected_total", "Asset entities detected in content")

# ── Decision / execution ─────────────────────
ai_decisions_total = Counter("ai_decisions_total", "Decisions evaluated", ["action"])
model_score_latency_ms = Histogram(
    "model_score_latency_ms", "Time to evaluate one symbol (ms)",
    buckets=(10, 25, 50, 100, 250, 500, 1000, 2500, 5000),
)
paper_orders_total = Counter("paper_orders_total", "Paper trades executed", ["side"])

# ── API (FastAPI request serving) ────────────
# Per-route latency + (via its _count) request totals, labelled by the matched
# route template (not the raw path) to keep cardinality bounded. Recorded by the
# http middleware in api/main.py; exposed on the API's /metrics endpoint.
api_request_duration_ms = Histogram(
    "api_request_duration_ms", "API request handling latency (ms)",
    ["method", "route", "status"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000),
)

# ── Worker liveness (generic) ────────────────
worker_events_processed_total = Counter(
    "worker_events_processed_total", "Events processed by a worker", ["worker"]
)
worker_events_failed_total = Counter(
    "worker_events_failed_total", "Errors encountered by a worker", ["worker"]
)
worker_last_success_ts = Gauge(
    "worker_last_success_ts", "Unix ts of the last successful worker cycle", ["worker"]
)


def start_metrics_server(port: int, enabled: bool = True) -> None:
    """Start the Prometheus exposition HTTP server (idempotent per process)."""
    if not enabled:
        logger.info("Metrics disabled; not starting server on port %s", port)
        return
    if not _PROM_AVAILABLE:
        return
    try:
        start_http_server(port)
        logger.info("Prometheus metrics server started on :%s", port)
    except Exception as e:  # pragma: no cover
        logger.warning("Could not start metrics server on :%s: %s", port, e)
