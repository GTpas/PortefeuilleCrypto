"""
Daily Report Worker  (scheduled advisory report over the universe)
==================================================================
Generates the Daily Crypto Intelligence Report once per day at the configured
local time (default 00:00), and on demand.

Data path (display/report-only — never touches the bot or persistence path):
  * reads the LIVE tiers from the local API (the same real-data endpoints the
    cockpit uses): ``/api/market/universe`` (≤300 Binance 24h rows) +
    ``/api/market/global`` (macro backdrop).
  * builds the report with ``reports.generator`` (numbers from ``reports.scoring``).
  * persists JSON + Markdown via ``reports.store`` (files = source of truth) and
    best-effort mirrors the index (+ per-asset scores) into Postgres.

Real data only: if the universe is empty/unavailable the run is recorded with
``status='error'`` and an explicit message — it never invents assets. Predictions
are prudent (probabilities/scenarios), enforced by the scoring layer.

Pure helpers (``next_run_at`` / ``resolve_tz``) are unit-tested offline.
Run directly: ``python -m workers.report_worker`` (scheduled loop) or
``python -m workers.report_worker --once`` (generate now and exit).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from reports import generator, store
from reports.store import ReportStore
from metrics import (
    start_metrics_server, daily_report_runs_total, daily_report_errors_total,
    daily_report_build_latency_ms, daily_report_assets, daily_report_last_success_ts,
    daily_report_signal_counts, worker_last_success_ts, worker_events_failed_total,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ReportWorker")


# ──────────────────────────────────────────────────────────────────────────────
# Pure scheduling helpers (offline-testable)
# ──────────────────────────────────────────────────────────────────────────────

def resolve_tz(name: str):
    """Resolve an IANA tz name to a tzinfo, falling back to UTC (Windows may lack
    the tz database unless ``tzdata`` is installed). Never raises."""
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as e:  # pragma: no cover - environment dependent
        logger.warning("Timezone %r unavailable (%s); using UTC.", name, e)
        return timezone.utc


def next_run_at(now: datetime, hour: int, minute: int, tz) -> datetime:
    """Next occurrence of hour:minute in ``tz`` strictly after ``now`` (tz-aware)."""
    now_local = now.astimezone(tz)
    target = datetime.combine(now_local.date(), dtime(hour=hour, minute=minute), tzinfo=tz)
    if target <= now_local:
        target = datetime.combine(now_local.date() + timedelta(days=1),
                                  dtime(hour=hour, minute=minute), tzinfo=tz)
    return target


def _now(tz) -> datetime:
    return datetime.now(timezone.utc).astimezone(tz)


# ──────────────────────────────────────────────────────────────────────────────
# Data fetch (from the local API — same real-data endpoints the cockpit uses)
# ──────────────────────────────────────────────────────────────────────────────

def _http_get_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": "antigravity-report/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (local trusted host)
        return json.loads(r.read().decode())


def fetch_universe_rows(api_base: str, limit: int, timeout: float) -> list[dict]:
    url = f"{api_base.rstrip('/')}/api/market/universe?limit={int(limit)}"
    data = _http_get_json(url, timeout)
    rows = data.get("rows") if isinstance(data, dict) else None
    return rows or []


def fetch_global_context(api_base: str, timeout: float) -> Optional[dict]:
    url = f"{api_base.rstrip('/')}/api/market/global"
    try:
        data = _http_get_json(url, timeout)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("Global context unavailable (continuing without macro): %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Generation (shared by the schedule loop and the --once path)
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_dir() -> str:
    d = settings.DAILY_REPORT_DIR
    if not os.path.isabs(d):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(root, d)
    return d


async def generate_report(*, trigger: str = "schedule", pool=None) -> dict:
    """Generate, persist, and (best-effort) DB-mirror one daily report.

    Returns the slim index entry. Raises only on a true generation failure
    (recorded with status='error' by the caller / DB mirror)."""
    daily_report_runs_total.labels(trigger=trigger).inc()
    tz = resolve_tz(settings.DAILY_REPORT_TIMEZONE)
    now_local = _now(tz)
    report_date = now_local.date().isoformat()
    generated_at = datetime.now(timezone.utc).isoformat()

    t0 = time.time()
    rows = await asyncio.to_thread(
        fetch_universe_rows, settings.DAILY_REPORT_API_BASE,
        settings.DAILY_REPORT_UNIVERSE_LIMIT, settings.DAILY_REPORT_HTTP_TIMEOUT)
    global_ctx = await asyncio.to_thread(
        fetch_global_context, settings.DAILY_REPORT_API_BASE, settings.DAILY_REPORT_HTTP_TIMEOUT)

    report = generator.build_daily_report(
        rows, global_ctx, generated_at=generated_at, report_date=report_date,
        top_n=settings.DAILY_REPORT_TOP_N)
    report["status"] = "ok" if rows else "error"
    if not rows:
        report["error_message"] = "universe unavailable (no real rows from /api/market/universe)"

    markdown = generator.render_markdown(report)
    rstore = ReportStore(_resolve_dir())
    entry = await asyncio.to_thread(rstore.save, report, markdown)
    report["_json_path"] = entry["json_path"]
    report["_markdown_path"] = entry["markdown_path"]

    latency_ms = (time.time() - t0) * 1000.0
    daily_report_build_latency_ms.observe(latency_ms)
    daily_report_assets.set(report.get("universe_size", 0))
    for sig, n in (report.get("signal_counts") or {}).items():
        daily_report_signal_counts.labels(signal=sig).set(n)

    await _mirror_db(report, pool)

    if rows:
        daily_report_last_success_ts.set(time.time())
        worker_last_success_ts.labels(worker="report_worker").set(time.time())
        logger.info("Daily report %s generated: %d assets, regime=%s, signals=%s in %.0fms",
                    report_date, report.get("universe_size", 0), report.get("market_regime"),
                    report.get("signal_counts"), latency_ms)
    else:
        daily_report_errors_total.inc()
        logger.error("Daily report %s generated with NO universe data (status=error).", report_date)
    return entry


async def _mirror_db(report: dict, pool) -> None:
    """Best-effort: write the index row (+ per-asset scores). Never fatal."""
    if not (settings.DAILY_REPORT_PERSIST_DB and pool is not None):
        return
    try:
        async with pool.acquire() as conn:
            await store.ensure_schema(conn)
            await store.upsert_index(conn, report, status=report.get("status", "ok"),
                                     error_message=report.get("error_message"))
            if report.get("assets"):
                await store.upsert_asset_scores(conn, report)
    except Exception as e:  # DB is a convenience mirror — files are the source of truth
        logger.warning("[reports] DB mirror failed (files still written): %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────

async def _make_pool():
    if not settings.DAILY_REPORT_PERSIST_DB:
        return None
    try:
        import asyncpg
        return await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=2)
    except Exception as e:
        logger.warning("[reports] DB pool unavailable (running file-only): %s", e)
        return None


async def run_report_worker() -> None:
    logger.info("Starting Daily Report Worker (tz=%s at %02d:%02d)…",
                settings.DAILY_REPORT_TIMEZONE, settings.DAILY_REPORT_HOUR,
                settings.DAILY_REPORT_MINUTE)
    start_metrics_server(settings.METRICS_PORT_REPORT, settings.METRICS_ENABLED)
    tz = resolve_tz(settings.DAILY_REPORT_TIMEZONE)
    pool = await _make_pool()

    try:
        while True:
            target = next_run_at(_now(tz), settings.DAILY_REPORT_HOUR,
                                 settings.DAILY_REPORT_MINUTE, tz)
            logger.info("Next daily report scheduled at %s", target.isoformat())
            # Sleep in chunks so cancellation stays responsive and clock drift /
            # suspend-resume is re-checked rather than trusted to one long sleep.
            while True:
                remaining = (target - _now(tz)).total_seconds()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, 300))
            try:
                await generate_report(trigger="schedule", pool=pool)
            except Exception as e:
                worker_events_failed_total.labels(worker="report_worker").inc()
                daily_report_errors_total.inc()
                logger.error("Daily report generation failed: %s", e)
            # Small guard so we don't double-fire within the same minute.
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("Report worker stopped.")
    finally:
        if pool is not None:
            await pool.close()


async def _run_once() -> None:
    start_metrics_server(settings.METRICS_PORT_REPORT, settings.METRICS_ENABLED)
    pool = await _make_pool()
    try:
        entry = await generate_report(trigger="manual", pool=pool)
        logger.info("One-shot report written: %s", entry.get("json_path"))
    finally:
        if pool is not None:
            await pool.close()


if __name__ == "__main__":
    once = "--once" in sys.argv
    try:
        asyncio.run(_run_once() if once else run_report_worker())
    except KeyboardInterrupt:
        logger.info("Report worker stopped by user.")
