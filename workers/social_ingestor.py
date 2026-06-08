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
    
    # Initialize components
    mock_collector = MockSocialCollector(pool)
    content_analyzer = ContentAnalyzer(pool)
    social_engine = SocialEngine(pool)
    
    cycle = 0
    
    try:
        while True:
            cycle += 1
            
            # ── Step 1: Collect social content ──
            try:
                ingested = await mock_collector.ingest(settings.ACTIVE_SYMBOLS)
                if ingested:
                    social_posts_collected_total.labels(source=mock_collector.source_name).inc(ingested)
            except Exception as e:
                logger.error(f"Social collection error: {e}")
                ingested = 0

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
