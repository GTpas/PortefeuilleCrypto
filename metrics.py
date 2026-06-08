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
