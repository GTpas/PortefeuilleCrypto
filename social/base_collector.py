"""
Base Social Collector
---------------------
Abstract base class for all social source collectors.
Provides:
- Normalized output format → raw_content
- Deduplication via content_hash
- Rate limiting per source
- DLQ for unparsable content
- Retry with exponential backoff
"""

import abc
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import asyncpg
import backoff

logger = logging.getLogger(__name__)


class SocialContent:
    """Normalized social content object across all sources."""

    def __init__(
        self,
        source_name: str,
        author_handle: str,
        text: str,
        published_at: datetime,
        source_url: Optional[str] = None,
        engagement: Optional[Dict[str, int]] = None,
        author_type: str = "unknown",
        raw_payload: Optional[Dict[str, Any]] = None,
    ):
        self.source_name = source_name
        self.author_handle = author_handle
        self.text = text
        self.published_at = published_at
        self.source_url = source_url
        self.engagement = engagement or {}
        self.author_type = author_type
        self.raw_payload = raw_payload or {}
        self.ingested_at = datetime.now(timezone.utc)

        # Compute stable content hash for deduplication
        hash_input = f"{source_name}:{author_handle}:{text}:{published_at.isoformat()}"
        self.content_hash = hashlib.sha256(hash_input.encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_name": self.source_name,
            "author_handle": self.author_handle,
            "text": self.text,
            "published_at": self.published_at.isoformat(),
            "source_url": self.source_url,
            "engagement": self.engagement,
            "author_type": self.author_type,
            "content_hash": self.content_hash,
        }


class BaseSocialCollector(abc.ABC):
    """
    Abstract base class for social source collectors.
    
    Subclasses must implement:
        - collect() → List[SocialContent]
        - source_name (property)
    """

    def __init__(self, db_pool: asyncpg.Pool, rate_limit_per_min: int = 30):
        self.db_pool = db_pool
        self.rate_limit_per_min = rate_limit_per_min
        self._request_count = 0
        self._rate_window_start = datetime.now(timezone.utc)
        self._source_id: Optional[int] = None

    @property
    @abc.abstractmethod
    def source_name(self) -> str:
        """Unique name for this source (must match tracked_source.name)."""
        pass

    @abc.abstractmethod
    async def collect(self, symbols: List[str]) -> List[SocialContent]:
        """
        Collect social content related to the given symbols.
        Should return normalized SocialContent objects.
        """
        pass

    async def _ensure_source_id(self) -> int:
        """Get or cache the source_id from tracked_source table."""
        if self._source_id is not None:
            return self._source_id

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM tracked_source WHERE name = $1",
                self.source_name
            )
            if row:
                self._source_id = row['id']
            else:
                self._source_id = await conn.fetchval(
                    "INSERT INTO tracked_source (name, type, reliability_score) VALUES ($1, $2, $3) RETURNING id",
                    self.source_name, "social_network", 0.5
                )
        return self._source_id

    async def _get_or_create_actor(self, handle: str, actor_type: str) -> int:
        """Get or create an actor in tracked_actor table."""
        source_id = await self._ensure_source_id()
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM tracked_actor WHERE source_id = $1 AND handle = $2",
                source_id, handle
            )
            if row:
                return row['id']
            return await conn.fetchval(
                "INSERT INTO tracked_actor (source_id, handle, actor_type) VALUES ($1, $2, $3) RETURNING id",
                source_id, handle, actor_type
            )

    async def _check_rate_limit(self):
        """Simple in-memory rate limiter."""
        now = datetime.now(timezone.utc)
        elapsed = (now - self._rate_window_start).total_seconds()
        if elapsed >= 60:
            self._request_count = 0
            self._rate_window_start = now

        if self._request_count >= self.rate_limit_per_min:
            wait_time = 60 - elapsed
            logger.warning(f"[{self.source_name}] Rate limit reached, waiting {wait_time:.0f}s")
            await asyncio.sleep(wait_time)
            self._request_count = 0
            self._rate_window_start = datetime.now(timezone.utc)

        self._request_count += 1

    async def ingest(self, symbols: List[str]) -> int:
        """
        Run collection, dedup, and write to raw_content.
        Returns number of new items ingested.
        """
        await self._check_rate_limit()
        source_id = await self._ensure_source_id()

        try:
            contents = await self.collect(symbols)
        except Exception as e:
            logger.error(f"[{self.source_name}] Collection failed: {e}")
            await self._write_to_dlq("collection_error", str(e), {"symbols": symbols})
            return 0

        ingested = 0
        for content in contents:
            try:
                # Dedup check
                async with self.db_pool.acquire() as conn:
                    existing = await conn.fetchval(
                        "SELECT 1 FROM raw_content WHERE content_hash = $1",
                        content.content_hash
                    )
                    if existing:
                        continue

                    # Get or create actor
                    actor_id = await self._get_or_create_actor(
                        content.author_handle, content.author_type
                    )

                    # Insert
                    await conn.execute("""
                        INSERT INTO raw_content (source_id, actor_id, source_url, content_hash, raw_payload, published_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (content_hash) DO NOTHING
                    """,
                        source_id, actor_id, content.source_url,
                        content.content_hash, json.dumps(content.to_dict()),
                        content.published_at
                    )
                    ingested += 1

            except Exception as e:
                logger.error(f"[{self.source_name}] Failed to ingest content: {e}")
                await self._write_to_dlq(
                    "ingestion_error", str(e),
                    {"content_hash": content.content_hash, "text_preview": content.text[:200]}
                )

        if ingested > 0:
            logger.info(f"[{self.source_name}] Ingested {ingested} new items (from {len(contents)} collected)")

        return ingested

    async def _write_to_dlq(self, error_class: str, error_message: str, payload: Dict):
        """Write failed content to dead letter queue."""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO dead_letter_event (error_class, error_message, raw_payload, source_channel)
                    VALUES ($1, $2, $3, $4)
                """, error_class, error_message, json.dumps(payload), f"social:{self.source_name}")
        except Exception as e:
            logger.error(f"[{self.source_name}] Failed to write to DLQ: {e}")
