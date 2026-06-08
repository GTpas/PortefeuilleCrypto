"""
Social Ingestor Worker
----------------------
Runs the mock social collector (or real collectors when available)
and the content analyzer in a continuous loop.
"""

import asyncio
import asyncpg
import logging
import sys
import os
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from social.mock_collector import MockSocialCollector
from social.content_analyzer import ContentAnalyzer
from signal_engine.social_engine import SocialEngine
from metrics import (
    start_metrics_server, social_posts_collected_total,
    content_analyzed_total, worker_last_success_ts,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SocialIngestor")


async def run_social_ingestor():
    logger.info("Starting Social Ingestor Worker...")

    start_metrics_server(settings.METRICS_PORT_SOCIAL, settings.METRICS_ENABLED)
    pool = await asyncpg.create_pool(settings.DATABASE_URL)

    # Build the list of REAL collectors. There is no real social connector yet,
    # so the only available collector is the simulated one, gated behind an
    # explicit dev flag. When no collector is enabled the worker stays alive but
    # writes NO social content/signals — the cockpit then honestly reports
    # "no real social feed configured" instead of fabricated data.
    collectors = []
    if settings.ENABLE_MOCK_SOCIAL:
        logger.warning(
            "ENABLE_MOCK_SOCIAL is ON — running the SIMULATED social collector. "
            "Its output is mock and must never be presented as real data."
        )
        collectors.append(MockSocialCollector(pool))

    content_analyzer = ContentAnalyzer(pool)
    social_engine = SocialEngine(pool)

    if not collectors:
        logger.warning(
            "No real social feed configured (ENABLE_MOCK_SOCIAL=False, no real "
            "connector wired). Social scoring is disabled; s_social will report "
            "as unavailable. Idling."
        )
        try:
            while True:
                worker_last_success_ts.labels(worker="social_ingestor").set(time.time())
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("Social ingestor stopped.")
        finally:
            await pool.close()
        return

    cycle = 0

    try:
        while True:
            cycle += 1

            # ── Step 1: Collect social content ──
            ingested = 0
            for collector in collectors:
                try:
                    n = await collector.ingest(settings.ACTIVE_SYMBOLS)
                    if n:
                        social_posts_collected_total.labels(source=collector.source_name).inc(n)
                        ingested += n
                except Exception as e:
                    logger.error(f"Social collection error ({collector.source_name}): {e}")

            # ── Step 2: Analyze new content ──
            try:
                analyzed = await content_analyzer.analyze_new_content(limit=200)
                if analyzed:
                    content_analyzed_total.inc(analyzed)
            except Exception as e:
                logger.error(f"Content analysis error: {e}")
                analyzed = 0

            # ── Step 3: Compute and write social signals ──
            for symbol in settings.ACTIVE_SYMBOLS:
                try:
                    result = await social_engine.compute_social_score(symbol)
                    # Only persist signals when the social data is actually present.
                    if result.get("available"):
                        await social_engine.write_social_signal(symbol, result)
                except Exception as e:
                    logger.error(f"Social signal computation error for {symbol}: {e}")

            if cycle % 10 == 0:
                logger.info(
                    f"Social ingestor cycle {cycle}: "
                    f"ingested={ingested}, analyzed={analyzed}, "
                    f"symbols={len(settings.ACTIVE_SYMBOLS)}"
                )
            
            worker_last_success_ts.labels(worker="social_ingestor").set(time.time())

            # Run every 10 seconds
            await asyncio.sleep(10)
            
    except asyncio.CancelledError:
        logger.info("Social ingestor stopped.")
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_social_ingestor())
    except KeyboardInterrupt:
        logger.info("Social ingestor stopped by user.")
