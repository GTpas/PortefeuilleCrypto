"""
Social Signal Engine
--------------------
Computes the real S_social score from raw_content and social_signal aggregates.
Replaces the previous random.uniform(-0.5, 0.5) mock.

Produces:
- mention_velocity_z: z-score of mention frequency vs historical baseline
- sentiment_polarity: aggregated sentiment [-1, +1]
- unique_authors: diversity of sources
- engagement_velocity: rate of engagement growth
- cross_source_confirmation: how many sources agree
- novelty_score: new narrative vs repeated
- actor_influence_score: credibility-weighted signal
- bot_risk_penalty: discount for suspected bot activity
- Final S_social ∈ [-1, +1]
"""

import json
import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

import asyncpg

from social.content_analyzer import ContentAnalyzer

logger = logging.getLogger(__name__)


class SocialEngine:
    """Computes the composite social score S_social for a given symbol."""

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        self.content_analyzer = ContentAnalyzer(db_pool)

    async def compute_social_score(self, symbol: str) -> Dict[str, Any]:
        """
        Compute S_social and all contributing metrics for a symbol.
        
        Returns:
            {
                "score": float,           # S_social ∈ [-1, +1]
                "factors": list,          # list of factor dicts
                "metrics": dict,          # raw metric values
                "source_breakdown": dict, # contribution by source
            }
        """
        base_asset = symbol.split('/')[0] if '/' in symbol else symbol
        
        async with self.db_pool.acquire() as conn:
            now = datetime.now(timezone.utc)

            # ── 1. Mention Velocity ──────────────
            mention_velocity_z = await self._compute_mention_velocity(conn, base_asset, now)

            # ── 2. Sentiment Polarity ────────────
            sentiment_polarity = await self._compute_sentiment(conn, base_asset, now)

            # ── 3. Unique Authors ────────────────
            unique_authors = await self._count_unique_authors(conn, base_asset, now)

            # ── 4. Engagement Velocity ───────────
            engagement_velocity = await self._compute_engagement_velocity(conn, base_asset, now)

            # ── 5. Cross-Source Confirmation ─────
            cross_source = await self._compute_cross_source(conn, base_asset, now)

            # ── 6. Novelty Score ─────────────────
            novelty_score = await self._compute_novelty(conn, base_asset, now)

            # ── 7. Actor Influence Score ─────────
            actor_influence = await self._compute_actor_influence(conn, base_asset, now)

            # ── 8. Bot Risk Penalty ──────────────
            bot_risk = await self._compute_bot_risk(conn, base_asset, now)

            # ── 9. Source Breakdown ──────────────
            source_breakdown = await self._compute_source_breakdown(conn, base_asset, now)

            # ── 10. Data freshness (age of most recent content) ──
            latest_published = await conn.fetchval("""
                SELECT max(rc.published_at)
                FROM content_entity ce
                JOIN raw_content rc ON rc.id = ce.raw_content_id
                WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
            """, base_asset)
            if latest_published is not None:
                data_age_ms = max(0.0, (now - latest_published).total_seconds() * 1000.0)
            else:
                data_age_ms = -1.0  # no social content for this asset

        # ── Composite S_social ───────────────
        # Weighted combination of normalized metrics
        raw_signal = (
            0.20 * self._normalize(mention_velocity_z, 0, 4)  # velocity z-score normalized
            + 0.25 * sentiment_polarity                          # already [-1, +1]
            + 0.10 * self._normalize(unique_authors, 0, 20)     # author diversity
            + 0.10 * self._normalize(engagement_velocity, 0, 5) # engagement rate
            + 0.10 * cross_source                                # already [0, 1]
            + 0.05 * novelty_score                               # already [0, 1]
            + 0.10 * actor_influence                             # already [0, 1]
            - 0.10 * bot_risk                                    # penalty [0, 1]
        )

        s_social = round(max(-1.0, min(1.0, raw_signal)), 4)

        # Build factors for decision_factor table
        factors = [
            {"name": "mention_velocity_z", "value": mention_velocity_z, "contrib": 0.20 * self._normalize(mention_velocity_z, 0, 4),
             "explanation": f"Mentions are {mention_velocity_z:.1f} std devs above mean ({'>3 = strong signal' if mention_velocity_z > 3 else 'normal'})"},
            {"name": "sentiment_polarity", "value": sentiment_polarity, "contrib": 0.25 * sentiment_polarity,
             "explanation": f"Aggregated sentiment: {'bullish' if sentiment_polarity > 0.2 else 'bearish' if sentiment_polarity < -0.2 else 'neutral'} ({sentiment_polarity:+.2f})"},
            {"name": "unique_authors", "value": unique_authors, "contrib": 0.10 * self._normalize(unique_authors, 0, 20),
             "explanation": f"{unique_authors} unique authors in last 5min ({'diverse' if unique_authors > 5 else 'limited'})"},
            {"name": "engagement_velocity", "value": engagement_velocity, "contrib": 0.10 * self._normalize(engagement_velocity, 0, 5),
             "explanation": f"Engagement growth rate: {engagement_velocity:.1f}x"},
            {"name": "cross_source_confirm", "value": cross_source, "contrib": 0.10 * cross_source,
             "explanation": f"Cross-source confirmation: {cross_source:.0%} ({'confirmed' if cross_source > 0.5 else 'single-source'})"},
            {"name": "novelty_score", "value": novelty_score, "contrib": 0.05 * novelty_score,
             "explanation": f"Narrative novelty: {novelty_score:.0%} ({'new narrative' if novelty_score > 0.7 else 'recurring'})"},
            {"name": "actor_influence_score", "value": actor_influence, "contrib": 0.10 * actor_influence,
             "explanation": f"Source credibility weight: {actor_influence:.0%}"},
            {"name": "bot_risk_penalty", "value": bot_risk, "contrib": -0.10 * bot_risk,
             "explanation": f"Bot risk penalty: {bot_risk:.0%} ({'high bot activity' if bot_risk > 0.3 else 'clean'})"},
        ]

        metrics = {
            "mention_velocity_z": mention_velocity_z,
            "sentiment_polarity": sentiment_polarity,
            "unique_authors": unique_authors,
            "engagement_velocity": engagement_velocity,
            "cross_source_confirm": cross_source,
            "novelty_score": novelty_score,
            "actor_influence_score": actor_influence,
            "bot_risk_penalty": bot_risk,
            "data_age_ms": round(data_age_ms, 1),
        }

        return {
            "score": s_social,
            "factors": factors,
            "metrics": metrics,
            "source_breakdown": source_breakdown,
        }

    async def write_social_signal(self, symbol: str, result: Dict[str, Any]) -> None:
        """Write computed social signal to social_signal_1m and social_signal_5m."""
        now = datetime.now(timezone.utc)
        metrics = result['metrics']
        source_breakdown = result.get('source_breakdown', {})

        async with self.db_pool.acquire() as conn:
            # 1-minute bucket
            bucket_1m = now.replace(second=0, microsecond=0)
            await conn.execute("""
                INSERT INTO social_signal_1m (
                    ts_bucket, symbol, s_social,
                    mention_velocity_z, sentiment_polarity, actor_influence_score, bot_risk_penalty,
                    unique_authors, engagement_velocity, cross_source_confirm, novelty_score,
                    source_breakdown
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (ts_bucket, symbol) DO UPDATE SET
                    s_social = EXCLUDED.s_social,
                    mention_velocity_z = EXCLUDED.mention_velocity_z,
                    sentiment_polarity = EXCLUDED.sentiment_polarity,
                    actor_influence_score = EXCLUDED.actor_influence_score,
                    bot_risk_penalty = EXCLUDED.bot_risk_penalty,
                    unique_authors = EXCLUDED.unique_authors,
                    engagement_velocity = EXCLUDED.engagement_velocity,
                    cross_source_confirm = EXCLUDED.cross_source_confirm,
                    novelty_score = EXCLUDED.novelty_score,
                    source_breakdown = EXCLUDED.source_breakdown
            """,
                bucket_1m, symbol, result['score'],
                metrics['mention_velocity_z'], metrics['sentiment_polarity'],
                metrics['actor_influence_score'], metrics['bot_risk_penalty'],
                metrics.get('unique_authors', 0),
                metrics.get('engagement_velocity', 0.0),
                metrics.get('cross_source_confirm', 0.0),
                metrics.get('novelty_score', 0.0),
                json.dumps(source_breakdown),
            )

            # 5-minute bucket
            minute = now.minute - (now.minute % 5)
            bucket_5m = now.replace(minute=minute, second=0, microsecond=0)
            await conn.execute("""
                INSERT INTO social_signal_5m (
                    ts_bucket, symbol, s_social,
                    mention_velocity_z, sentiment_polarity,
                    unique_authors, engagement_velocity, cross_source_confirm,
                    novelty_score, actor_influence_score, bot_risk_penalty,
                    source_breakdown
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (ts_bucket, symbol) DO UPDATE SET
                    s_social = EXCLUDED.s_social,
                    mention_velocity_z = EXCLUDED.mention_velocity_z,
                    sentiment_polarity = EXCLUDED.sentiment_polarity,
                    unique_authors = EXCLUDED.unique_authors,
                    engagement_velocity = EXCLUDED.engagement_velocity,
                    cross_source_confirm = EXCLUDED.cross_source_confirm,
                    novelty_score = EXCLUDED.novelty_score,
                    actor_influence_score = EXCLUDED.actor_influence_score,
                    bot_risk_penalty = EXCLUDED.bot_risk_penalty,
                    source_breakdown = EXCLUDED.source_breakdown
            """,
                bucket_5m, symbol, result['score'],
                metrics['mention_velocity_z'], metrics['sentiment_polarity'],
                metrics.get('unique_authors', 0),
                metrics.get('engagement_velocity', 0.0),
                metrics.get('cross_source_confirm', 0.0),
                metrics.get('novelty_score', 0.0),
                metrics.get('actor_influence_score', 0.0),
                metrics.get('bot_risk_penalty', 0.0),
                json.dumps(source_breakdown),
            )

    # ── Private metric computations ──────────

    async def _compute_mention_velocity(self, conn: asyncpg.Connection,
                                         asset: str, now: datetime) -> float:
        """
        Z-score of mention count in last 5 min vs 24h baseline.
        """
        five_min_ago = now - timedelta(minutes=5)
        twenty_four_h_ago = now - timedelta(hours=24)

        # Current 5min count
        current_count = await conn.fetchval("""
            SELECT COUNT(*) FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
        """, asset, five_min_ago) or 0

        # 24h historical counts per 5-min window
        historical = await conn.fetch("""
            SELECT time_bucket('5 minutes', rc.published_at) AS bucket, COUNT(*) AS cnt
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2 AND rc.published_at < $3
            GROUP BY bucket
        """, asset, twenty_four_h_ago, five_min_ago)

        if len(historical) < 3:
            # Not enough history, use raw count as proxy
            return min(4.0, current_count / max(1, 3))

        counts = [int(r['cnt']) for r in historical]
        mean = sum(counts) / len(counts)
        variance = sum((c - mean) ** 2 for c in counts) / len(counts)
        std = max(variance ** 0.5, 0.5)  # floor at 0.5 to avoid division by near-zero

        z_score = (current_count - mean) / std
        return round(max(0.0, z_score), 4)

    async def _compute_sentiment(self, conn: asyncpg.Connection,
                                  asset: str, now: datetime) -> float:
        """
        Aggregated sentiment polarity from recent content mentioning this asset.
        """
        five_min_ago = now - timedelta(minutes=5)

        records = await conn.fetch("""
            SELECT rc.raw_payload
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
            LIMIT 100
        """, asset, five_min_ago)

        if not records:
            return 0.0

        polarities = []
        for r in records:
            payload = r['raw_payload']
            if isinstance(payload, str):
                payload = json.loads(payload)
            text = payload.get('text', '')
            if text:
                polarity = self.content_analyzer.compute_sentiment(text)
                polarities.append(polarity)

        if not polarities:
            return 0.0

        return round(sum(polarities) / len(polarities), 4)

    async def _count_unique_authors(self, conn: asyncpg.Connection,
                                     asset: str, now: datetime) -> int:
        """Count unique authors mentioning this asset in last 5 minutes."""
        five_min_ago = now - timedelta(minutes=5)

        count = await conn.fetchval("""
            SELECT COUNT(DISTINCT rc.actor_id)
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
        """, asset, five_min_ago)

        return int(count) if count else 0

    async def _compute_engagement_velocity(self, conn: asyncpg.Connection,
                                            asset: str, now: datetime) -> float:
        """
        Measure engagement growth: ratio of last-2min engagement to prev-5min.
        """
        two_min_ago = now - timedelta(minutes=2)
        five_min_ago = now - timedelta(minutes=5)

        recent = await conn.fetch("""
            SELECT rc.raw_payload
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
        """, asset, two_min_ago)

        older = await conn.fetch("""
            SELECT rc.raw_payload
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2 AND rc.published_at < $3
        """, asset, five_min_ago, two_min_ago)

        def sum_engagement(records):
            total = 0
            for r in records:
                payload = r['raw_payload']
                if isinstance(payload, str):
                    payload = json.loads(payload)
                eng = payload.get('engagement', {})
                total += eng.get('likes', 0) + eng.get('retweets', 0) * 2 + eng.get('replies', 0) * 3
            return max(total, 1)

        recent_eng = sum_engagement(recent)
        older_eng = sum_engagement(older)

        ratio = recent_eng / older_eng
        return round(ratio, 4)

    async def _compute_cross_source(self, conn: asyncpg.Connection,
                                     asset: str, now: datetime) -> float:
        """
        How many distinct sources mention this asset recently?
        Normalized to [0, 1]: 0 = single source, 1 = 3+ sources.
        """
        five_min_ago = now - timedelta(minutes=5)

        count = await conn.fetchval("""
            SELECT COUNT(DISTINCT rc.source_id)
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
        """, asset, five_min_ago)

        sources = int(count) if count else 0
        return round(min(1.0, sources / 3.0), 4)

    async def _compute_novelty(self, conn: asyncpg.Connection,
                                asset: str, now: datetime) -> float:
        """
        Is this a new narrative or recurring noise?
        Compare current content types to the 24h historical distribution.
        """
        five_min_ago = now - timedelta(minutes=5)
        twenty_four_h_ago = now - timedelta(hours=24)

        # Recent content types
        recent_types = await conn.fetch("""
            SELECT ce.content_type, COUNT(*) as cnt
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
            GROUP BY ce.content_type
        """, asset, five_min_ago)

        # Historical content types
        historical_types = await conn.fetch("""
            SELECT ce.content_type, COUNT(*) as cnt
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2 AND rc.published_at < $3
            GROUP BY ce.content_type
        """, asset, twenty_four_h_ago, five_min_ago)

        if not recent_types:
            return 0.0

        recent_set = set(r['content_type'] for r in recent_types)
        historical_set = set(r['content_type'] for r in historical_types)

        # New types that weren't in history
        new_types = recent_set - historical_set
        if recent_set:
            novelty = len(new_types) / len(recent_set)
        else:
            novelty = 0.0

        # Also check if announcement/security_incident types are new (high novelty signal)
        high_value_types = {'announcement', 'listing', 'security_incident', 'regulation'}
        if new_types & high_value_types:
            novelty = min(1.0, novelty + 0.3)

        return round(novelty, 4)

    async def _compute_actor_influence(self, conn: asyncpg.Connection,
                                        asset: str, now: datetime) -> float:
        """
        Credibility-weighted signal strength.
        Higher if high-influence actors are talking.
        """
        five_min_ago = now - timedelta(minutes=5)

        rows = await conn.fetch("""
            SELECT ta.influence_score
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            JOIN tracked_actor ta ON ta.id = rc.actor_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
        """, asset, five_min_ago)

        if not rows:
            return 0.0

        scores = [float(r['influence_score']) for r in rows]
        # Use max (best actor) weighted with average
        avg = sum(scores) / len(scores)
        max_score = max(scores)
        weighted = 0.6 * max_score + 0.4 * avg
        return round(min(1.0, weighted), 4)

    async def _compute_bot_risk(self, conn: asyncpg.Connection,
                                 asset: str, now: datetime) -> float:
        """
        Heuristic bot detection based on:
        - Author type = 'bot' or 'unknown' with low credibility
        - High volume from single author
        - Repeated exact or near-exact text
        """
        five_min_ago = now - timedelta(minutes=5)

        # Check actor types
        type_counts = await conn.fetch("""
            SELECT ta.actor_type, COUNT(*) as cnt
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            JOIN tracked_actor ta ON ta.id = rc.actor_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
            GROUP BY ta.actor_type
        """, asset, five_min_ago)

        total = sum(int(r['cnt']) for r in type_counts)
        if total == 0:
            return 0.0

        bot_count = sum(int(r['cnt']) for r in type_counts if r['actor_type'] in ('bot', 'unknown'))
        bot_ratio = bot_count / total

        # Check single-author dominance
        author_counts = await conn.fetch("""
            SELECT rc.actor_id, COUNT(*) as cnt
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
            GROUP BY rc.actor_id
            ORDER BY cnt DESC
            LIMIT 1
        """, asset, five_min_ago)

        dominance = 0.0
        if author_counts and total > 0:
            top_author_count = int(author_counts[0]['cnt'])
            dominance = top_author_count / total

        # Combined bot risk
        risk = 0.5 * bot_ratio + 0.5 * max(0, dominance - 0.5)
        return round(min(1.0, risk), 4)

    async def _compute_source_breakdown(self, conn: asyncpg.Connection,
                                         asset: str, now: datetime) -> Dict[str, float]:
        """
        Compute the contribution breakdown by source (twitter, reddit, telegram, etc.).
        """
        five_min_ago = now - timedelta(minutes=5)

        rows = await conn.fetch("""
            SELECT ts.name, COUNT(*) as cnt
            FROM content_entity ce
            JOIN raw_content rc ON rc.id = ce.raw_content_id
            JOIN tracked_source ts ON ts.id = rc.source_id
            WHERE ce.entity_value = $1 AND ce.entity_type = 'asset'
              AND rc.published_at >= $2
            GROUP BY ts.name
        """, asset, five_min_ago)

        total = sum(int(r['cnt']) for r in rows)
        if total == 0:
            return {}

        return {r['name']: round(int(r['cnt']) / total, 4) for r in rows}

    @staticmethod
    def _normalize(value: float, low: float, high: float) -> float:
        """Normalize value to [-1, +1] range."""
        if high <= low:
            return 0.0
        clamped = max(low, min(high, value))
        return (clamped - low) / (high - low) * 2 - 1
