"""
Content Analyzer
----------------
Extracts entities, classifies content types, and scores confidence
from raw_content entries. Writes results to content_entity table.
"""

import re
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

import asyncpg

logger = logging.getLogger(__name__)

# ── Asset detection ──────────────────────────
# Common crypto tickers and aliases
ASSET_ALIASES = {
    "BTC": ["bitcoin", "btc", "₿", "xbt"],
    "ETH": ["ethereum", "eth", "ether"],
    "SOL": ["solana", "sol"],
    "BNB": ["binance coin", "bnb"],
    "XRP": ["ripple", "xrp"],
    "ADA": ["cardano", "ada"],
    "DOGE": ["dogecoin", "doge"],
    "AVAX": ["avalanche", "avax"],
    "DOT": ["polkadot", "dot"],
    "MATIC": ["polygon", "matic"],
    "LINK": ["chainlink", "link"],
    "UNI": ["uniswap", "uni"],
    "AAVE": ["aave"],
    "ATOM": ["cosmos", "atom"],
}

# ── Content type patterns ────────────────────
CONTENT_TYPE_PATTERNS = {
    "announcement": [
        r"official[ly]*", r"announc", r"confirm", r"partnership",
        r"mainnet", r"upgrade", r"launch", r"release", r"foundation",
    ],
    "listing": [
        r"list(?:ing|ed)", r"added to", r"now available on", r"trading pair",
    ],
    "security_incident": [
        r"hack", r"exploit", r"vulnerability", r"breach", r"stolen",
        r"drained", r"compromised", r"attack",
    ],
    "governance": [
        r"governance", r"proposal", r"vote", r"dao", r"treasury",
    ],
    "regulation": [
        r"sec\b", r"regulat", r"complian", r"legal", r"lawsuit",
        r"enforcement", r"ban", r"restrict",
    ],
    "rumor": [
        r"rumor", r"unconfirmed", r"allegedly", r"reportedly",
        r"sources say", r"can't verify",
    ],
    "hype": [
        r"🚀", r"moon", r"to the moon", r"pump", r"lfg",
        r"wagmi", r"bullish af", r"loaded up",
    ],
    "market_commentary": [
        r"support", r"resistance", r"breakout", r"consolidat",
        r"accumulation", r"distribution", r"volume",
    ],
}

# ── Sentiment lexicon ────────────────────────
BULLISH_WORDS = {
    "bullish", "buy", "long", "accumulate", "moon", "breakout", "pump",
    "upgrade", "partnership", "adoption", "institutional", "record",
    "strong", "incredible", "game changer", "explosion", "soaring",
    "surge", "rally", "ath", "all-time high", "undervalued",
}

BEARISH_WORDS = {
    "bearish", "sell", "short", "dump", "crash", "weak", "decline",
    "exploit", "hack", "scam", "rug", "collapse", "plunge", "capitulation",
    "overvalued", "bubble", "crackdown", "delisting", "ban", "arrested",
    "crumbling", "distribution", "insider selling",
}


class ContentAnalyzer:
    """Analyzes raw_content entries and extracts structured entities."""

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        # Pre-compile regex patterns
        self._asset_patterns = {}
        for asset, aliases in ASSET_ALIASES.items():
            patterns = [re.compile(rf'\b{re.escape(a)}\b', re.IGNORECASE) for a in aliases]
            patterns.append(re.compile(rf'\b{re.escape(asset)}\b'))  # exact ticker (case sensitive)
            self._asset_patterns[asset] = patterns

        self._content_type_patterns = {}
        for ctype, patterns in CONTENT_TYPE_PATTERNS.items():
            self._content_type_patterns[ctype] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

    def detect_assets(self, text: str) -> List[Tuple[str, float]]:
        """
        Detect mentioned crypto assets in text.
        Returns list of (asset, confidence) tuples.
        """
        results = []
        text_lower = text.lower()

        for asset, patterns in self._asset_patterns.items():
            match_count = 0
            for pattern in patterns:
                if pattern.search(text):
                    match_count += 1

            if match_count > 0:
                # Confidence based on how many aliases match
                confidence = min(1.0, 0.5 + match_count * 0.2)
                results.append((asset, round(confidence, 2)))

        return results

    def classify_content_type(self, text: str) -> Tuple[str, float]:
        """
        Classify the content type based on keyword patterns.
        Returns (content_type, confidence).
        """
        scores = {}
        for ctype, patterns in self._content_type_patterns.items():
            match_count = sum(1 for p in patterns if p.search(text))
            if match_count > 0:
                scores[ctype] = match_count

        if not scores:
            return "market_commentary", 0.3  # default

        best_type = max(scores, key=scores.get)
        confidence = min(1.0, 0.4 + scores[best_type] * 0.15)
        return best_type, round(confidence, 2)

    def compute_sentiment(self, text: str) -> float:
        """
        Rule-based sentiment polarity.
        Returns value in [-1, +1]: +1 = very bullish, -1 = very bearish.
        """
        text_lower = text.lower()
        words = set(re.findall(r'\b\w+\b', text_lower))

        # Check multi-word patterns
        bullish_count = sum(1 for w in BULLISH_WORDS if w in text_lower)
        bearish_count = sum(1 for w in BEARISH_WORDS if w in text_lower)

        # Check emoji sentiment
        if "🚀" in text:
            bullish_count += 2
        if "🔴" in text or "🚩" in text:
            bearish_count += 1

        total = bullish_count + bearish_count
        if total == 0:
            return 0.0

        polarity = (bullish_count - bearish_count) / total
        return round(max(-1.0, min(1.0, polarity)), 4)

    async def analyze_new_content(self, limit: int = 100) -> int:
        """
        Process unanalyzed raw_content entries and write content_entity records.
        Returns number of content items processed.
        """
        async with self.db_pool.acquire() as conn:
            # Find raw_content without entities yet
            records = await conn.fetch("""
                SELECT rc.id, rc.raw_payload, rc.published_at
                FROM raw_content rc
                LEFT JOIN content_entity ce ON ce.raw_content_id = rc.id
                WHERE ce.id IS NULL
                ORDER BY rc.ingested_at DESC
                LIMIT $1
            """, limit)

            processed = 0
            for record in records:
                try:
                    payload = record['raw_payload']
                    if isinstance(payload, str):
                        payload = json.loads(payload)

                    text = payload.get('text', '')
                    if not text:
                        continue

                    # Detect assets
                    assets = self.detect_assets(text)
                    content_type, type_confidence = self.classify_content_type(text)

                    for asset, confidence in assets:
                        await conn.execute("""
                            INSERT INTO content_entity (raw_content_id, entity_type, entity_value, entity_confidence, content_type)
                            VALUES ($1, 'asset', $2, $3, $4)
                        """, record['id'], asset, confidence, content_type)

                    # If no assets detected, still record the content type
                    if not assets:
                        await conn.execute("""
                            INSERT INTO content_entity (raw_content_id, entity_type, entity_value, entity_confidence, content_type)
                            VALUES ($1, 'unknown', 'no_asset_detected', $2, $3)
                        """, record['id'], type_confidence, content_type)

                    processed += 1

                except Exception as e:
                    logger.error(f"Content analysis error for raw_content #{record['id']}: {e}")

            if processed > 0:
                logger.info(f"Analyzed {processed} new content items")

            return processed
