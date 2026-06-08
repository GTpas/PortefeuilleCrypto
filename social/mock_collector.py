"""
Mock Social Collector
---------------------
Generates realistic simulated social content for testing the full pipeline.
Simulates:
- Multiple authors with varying credibility
- Attention shocks (sudden spikes in mentions)
- Mixed sentiments (bullish, bearish, neutral, FUD)
- Different content types (announcement, rumor, hype, etc.)
"""

import asyncio
import random
import string
from datetime import datetime, timezone, timedelta
from typing import List

import asyncpg

from social.base_collector import BaseSocialCollector, SocialContent

import logging
logger = logging.getLogger(__name__)


# Realistic author personas
MOCK_AUTHORS = [
    {"handle": "@crypto_whale_99", "type": "influencer", "credibility": 0.4},
    {"handle": "@eth_foundation", "type": "protocol_official", "credibility": 0.9},
    {"handle": "@btc_maxi_chad", "type": "influencer", "credibility": 0.3},
    {"handle": "@defi_researcher", "type": "researcher", "credibility": 0.8},
    {"handle": "@exchange_news", "type": "media", "credibility": 0.7},
    {"handle": "@altcoin_signals", "type": "influencer", "credibility": 0.2},
    {"handle": "@sec_watchdog", "type": "regulatory", "credibility": 0.85},
    {"handle": "@vitalik_fan", "type": "influencer", "credibility": 0.35},
    {"handle": "@binance_official", "type": "exchange", "credibility": 0.9},
    {"handle": "@random_trader_42", "type": "unknown", "credibility": 0.15},
    {"handle": "@crypto_bot_001", "type": "bot", "credibility": 0.05},
    {"handle": "@institutional_desk", "type": "market_maker", "credibility": 0.8},
]

# Template sentences with {asset} placeholder
BULLISH_TEMPLATES = [
    "Just loaded up on {asset}. Technical setup looks incredible. 🚀",
    "{asset} breaking out of a multi-week consolidation. Accumulation zone confirmed.",
    "Institutional inflows into {asset} ETF hit record highs. Extremely bullish.",
    "Major protocol upgrade for {asset} coming next week. Game changer.",
    "{asset} whale wallets accumulating heavily. On-chain data is screaming buy.",
    "Confirmed: {asset} listing on major exchange. Liquidity about to explode.",
    "{asset} hash rate at all-time high. Network is stronger than ever.",
]

BEARISH_TEMPLATES = [
    "{asset} looks weak here. Support levels crumbling. Reducing exposure.",
    "Massive {asset} transfer to exchange wallets. Selling pressure incoming.",
    "Regulatory crackdown concerns around {asset}. Caution advised.",
    "{asset} team wallet moving tokens. Insider selling? 🚩",
    "Shorts on {asset} increasing rapidly. Bears in control.",
    "{asset} volume declining while price consolidates. Distribution pattern.",
]

NEUTRAL_TEMPLATES = [
    "{asset} trading in a tight range. Waiting for a clear direction.",
    "Interesting developments around {asset} governance proposal. Worth watching.",
    "Mixed signals on {asset}. Social sentiment diverges from price action.",
    "{asset} dev activity remains steady. No major changes expected short-term.",
]

FUD_TEMPLATES = [
    "BREAKING: {asset} exploit reported! Millions at risk! (unconfirmed)",
    "Rumors of {asset} exchange delisting circulating. Can't verify yet.",
    "{asset} team member allegedly arrested. Details unclear. 🔴",
]

ANNOUNCEMENT_TEMPLATES = [
    "Official: {asset} mainnet upgrade scheduled for next Thursday.",
    "{asset} foundation announces $50M ecosystem fund.",
    "Partnership confirmed between {asset} and major fintech company.",
]

SOURCES = ["twitter", "reddit", "telegram"]


class MockSocialCollector(BaseSocialCollector):
    """
    Generates realistic mock social data for pipeline testing.
    Produces 3-12 posts per collection cycle, with occasional attention shocks.
    """

    def __init__(self, db_pool: asyncpg.Pool, shock_probability: float = 0.08):
        super().__init__(db_pool, rate_limit_per_min=120)
        self.shock_probability = shock_probability
        self._shock_asset = None
        self._shock_remaining = 0

    @property
    def source_name(self) -> str:
        return "mock_social"

    async def collect(self, symbols: List[str]) -> List[SocialContent]:
        contents = []

        # Check for attention shock
        if self._shock_remaining > 0:
            self._shock_remaining -= 1
            # During shock: 15-30 posts focused on one asset
            num_posts = random.randint(15, 30)
            shock_asset = self._shock_asset
        elif random.random() < self.shock_probability:
            # Start a new shock
            base_assets = [s.split('/')[0] for s in symbols]
            self._shock_asset = random.choice(base_assets)
            self._shock_remaining = random.randint(3, 8)  # shock lasts 3-8 cycles
            num_posts = random.randint(15, 30)
            shock_asset = self._shock_asset
            logger.info(f"[MockSocial] 🚨 Attention shock started for {shock_asset} ({self._shock_remaining} cycles)")
        else:
            num_posts = random.randint(3, 12)
            shock_asset = None

        for _ in range(num_posts):
            # Pick asset
            base_assets = [s.split('/')[0] for s in symbols]
            if shock_asset and random.random() < 0.7:
                asset = shock_asset
            else:
                asset = random.choice(base_assets)

            # Pick sentiment and template
            sentiment_roll = random.random()
            if shock_asset and asset == shock_asset:
                # During shock, sentiment is more extreme
                if sentiment_roll < 0.5:
                    template = random.choice(BULLISH_TEMPLATES)
                elif sentiment_roll < 0.75:
                    template = random.choice(ANNOUNCEMENT_TEMPLATES)
                elif sentiment_roll < 0.9:
                    template = random.choice(FUD_TEMPLATES)
                else:
                    template = random.choice(BEARISH_TEMPLATES)
            else:
                if sentiment_roll < 0.35:
                    template = random.choice(BULLISH_TEMPLATES)
                elif sentiment_roll < 0.55:
                    template = random.choice(BEARISH_TEMPLATES)
                elif sentiment_roll < 0.8:
                    template = random.choice(NEUTRAL_TEMPLATES)
                elif sentiment_roll < 0.92:
                    template = random.choice(ANNOUNCEMENT_TEMPLATES)
                else:
                    template = random.choice(FUD_TEMPLATES)

            text = template.format(asset=asset)

            # Pick author
            author = random.choice(MOCK_AUTHORS)

            # Pick source
            source = random.choice(SOURCES)

            # Timestamp: within last 2 minutes
            published_at = datetime.now(timezone.utc) - timedelta(
                seconds=random.randint(0, 120)
            )

            # Engagement metrics
            base_engagement = random.randint(1, 100)
            if author['credibility'] > 0.7:
                base_engagement *= random.randint(5, 20)

            engagement = {
                "likes": base_engagement,
                "retweets": int(base_engagement * random.uniform(0.1, 0.5)),
                "replies": int(base_engagement * random.uniform(0.05, 0.3)),
            }

            content = SocialContent(
                source_name=source,
                author_handle=author['handle'],
                text=text,
                published_at=published_at,
                source_url=f"https://mock.social/{source}/{''.join(random.choices(string.ascii_lowercase, k=8))}",
                engagement=engagement,
                author_type=author['type'],
                raw_payload={
                    "mock": True,
                    "credibility": author['credibility'],
                    "asset_mentioned": asset,
                    "is_shock": shock_asset == asset if shock_asset else False,
                },
            )
            contents.append(content)

        return contents
