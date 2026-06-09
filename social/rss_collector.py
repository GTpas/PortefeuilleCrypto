"""
RSS / Atom News Collector  (first REAL social source)
------------------------------------------------------
The first genuine, non-mock connector behind ``BaseSocialCollector``. It polls
**public crypto-news RSS/Atom feeds** — these are published explicitly for
syndication, so reading them is ToS-safe (unlike scraping X/Reddit/Telegram,
which is gated and legally risky).

Design notes
------------
- ``parse_feed`` is a **pure** function (bytes -> List[SocialContent]); it has
  no I/O, so the parsing is unit-tested offline (``tests/test_rss_collector``).
- Output is **REAL** content: ``source_name`` never starts with "mock", so the
  API's ``NOT ILIKE 'mock%'`` evidence filter keeps it, and the social engine
  treats the resulting S_social as real (not "unavailable").
- Politeness: a single collector polls at most once every ``RSS_POLL_SECONDS``;
  in-between cycles ``collect()`` returns ``[]`` so the 10 s social loop does
  not hammer the feeds.
- Per-item assets are detected downstream by ``ContentAnalyzer.detect_assets``;
  ``collect(symbols)`` does not pre-filter, because news is global.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from xml.etree import ElementTree as ET

import asyncpg
import httpx

from social.base_collector import BaseSocialCollector, SocialContent

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Atom namespace (RSS 2.0 has no namespace on its elements).
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _strip_html(text: str) -> str:
    """Crude HTML→text: drop tags and collapse whitespace (feeds embed markup)."""
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    """Parse RFC-822 (RSS pubDate) or ISO-8601 (Atom). Returns tz-aware UTC."""
    if not raw:
        return None
    raw = raw.strip()
    # RFC-822, e.g. "Mon, 09 Jun 2026 13:45:00 +0000"
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    # ISO-8601 / Atom, e.g. "2026-06-09T13:45:00Z"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _findtext(el, *tags: str) -> Optional[str]:
    """First non-empty text among the given child tags (namespace-tolerant)."""
    for tag in tags:
        child = el.find(tag)
        if child is not None and child.text:
            return child.text
    return None


def parse_feed(content: bytes, source_name: str) -> List[SocialContent]:
    """
    Parse an RSS 2.0 or Atom feed into normalized SocialContent objects.

    Pure (no I/O). Malformed entries are skipped, not raised, so one bad item
    never sinks the whole feed.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.warning("[%s] RSS parse error: %s", source_name, e)
        return []

    items = root.iter("item")  # RSS 2.0
    out: List[SocialContent] = []

    feed_title = None
    channel = root.find("channel")
    if channel is not None:
        feed_title = _findtext(channel, "title")

    found_rss = False
    for item in items:
        found_rss = True
        _append_item(out, item, source_name, feed_title, atom=False)

    if not found_rss:
        # Atom: <feed><entry>…</entry></feed>
        feed_title = _findtext(root, f"{_ATOM_NS}title") or feed_title
        for entry in root.iter(f"{_ATOM_NS}entry"):
            _append_item(out, entry, source_name, feed_title, atom=True)

    return out


def _append_item(out: List[SocialContent], el, source_name: str,
                 feed_title: Optional[str], atom: bool) -> None:
    try:
        if atom:
            title = _findtext(el, f"{_ATOM_NS}title")
            summary = _findtext(el, f"{_ATOM_NS}summary", f"{_ATOM_NS}content")
            link_el = el.find(f"{_ATOM_NS}link")
            link = link_el.get("href") if link_el is not None else None
            published = _findtext(el, f"{_ATOM_NS}published", f"{_ATOM_NS}updated")
            author_el = el.find(f"{_ATOM_NS}author")
            author = _findtext(author_el, f"{_ATOM_NS}name") if author_el is not None else None
        else:
            title = _findtext(el, "title")
            summary = _findtext(el, "description")
            link = _findtext(el, "link", "guid")
            published = _findtext(el, "pubDate", "{http://purl.org/dc/elements/1.1/}date")
            author = _findtext(el, "author", "{http://purl.org/dc/elements/1.1/}creator")

        text = _strip_html(" ".join(p for p in (title, summary) if p))
        if not text:
            return

        published_at = _parse_date(published) or datetime.now(timezone.utc)
        # Author handle keeps feed diversity meaningful for unique_authors:
        # prefer the byline, fall back to the feed/source name.
        handle = (author or feed_title or source_name).strip()[:200]

        out.append(SocialContent(
            source_name=source_name,
            author_handle=handle,
            text=text[:2000],
            published_at=published_at,
            source_url=link,
            engagement={},               # RSS exposes no engagement — honestly empty
            author_type="media",
            raw_payload={"feed": feed_title, "title": title, "link": link},
        ))
    except Exception as e:  # one bad item must not sink the feed
        logger.debug("[%s] skipped malformed RSS item: %s", source_name, e)


class RSSCollector(BaseSocialCollector):
    """
    Polls a set of public RSS/Atom news feeds and emits real SocialContent.

    One collector ↔ one ``tracked_source`` row (``source_name``). Group feeds by
    publisher if you want per-source attribution in cross-source confirmation.
    """

    def __init__(self, db_pool: asyncpg.Pool, feed_urls: List[str],
                 source_name: str = "rss_news", rate_limit_per_min: int = 30,
                 timeout: float = 10.0, poll_seconds: int = 120):
        super().__init__(db_pool, rate_limit_per_min=rate_limit_per_min)
        self.feed_urls = list(feed_urls or [])
        self._source_name = source_name
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self._last_poll_monotonic: float = 0.0

    @property
    def source_name(self) -> str:
        return self._source_name

    def _due(self) -> bool:
        """Politeness gate: only fetch once per poll_seconds."""
        now = time.monotonic()
        if self._last_poll_monotonic and (now - self._last_poll_monotonic) < self.poll_seconds:
            return False
        self._last_poll_monotonic = now
        return True

    async def collect(self, symbols: List[str]) -> List[SocialContent]:
        if not self.feed_urls or not self._due():
            return []

        contents: List[SocialContent] = []
        headers = {"User-Agent": "PortefeuilleCrypto/1.0 (+local paper-trading research)"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers,
                                     follow_redirects=True) as client:
            results = await asyncio.gather(
                *[self._fetch_one(client, url) for url in self.feed_urls],
                return_exceptions=True,
            )
        for url, res in zip(self.feed_urls, results):
            if isinstance(res, Exception):
                logger.warning("[%s] feed fetch failed (%s): %s", self.source_name, url, res)
                continue
            contents.extend(res)
        return contents

    async def _fetch_one(self, client: httpx.AsyncClient, url: str) -> List[SocialContent]:
        resp = await client.get(url)
        resp.raise_for_status()
        return parse_feed(resp.content, self.source_name)
