"""
Offline tests for the real RSS news collector.

Pin the parsing contract (RSS 2.0 + Atom), the "never mock" naming guarantee,
the HTML/whitespace cleanup, robust date handling, and the politeness gate.
No network or DB is touched — parse_feed is pure and _due is time-only.
"""

from datetime import datetime, timezone

from social.rss_collector import RSSCollector, parse_feed, _strip_html, _parse_date


RSS_2_0 = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>CryptoWire</title>
    <item>
      <title>Bitcoin ETF sees record inflows</title>
      <description>&lt;p&gt;Institutional demand for &lt;b&gt;BTC&lt;/b&gt; surges.&lt;/p&gt;</description>
      <link>https://example.com/btc-etf</link>
      <author>Jane Reporter</author>
      <pubDate>Mon, 09 Jun 2026 13:45:00 +0000</pubDate>
    </item>
    <item>
      <title>Ethereum upgrade scheduled</title>
      <description>Mainnet upgrade next week.</description>
      <link>https://example.com/eth-upgrade</link>
      <pubDate>Mon, 09 Jun 2026 12:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>DecryptFeed</title>
  <entry>
    <title>Solana network hits new high</title>
    <summary>SOL throughput at all-time high.</summary>
    <link href="https://example.com/sol"/>
    <published>2026-06-09T10:30:00Z</published>
    <author><name>Atom Author</name></author>
  </entry>
</feed>
"""


def test_parses_rss_2_0_items():
    items = parse_feed(RSS_2_0, "rss_news")
    assert len(items) == 2
    first = items[0]
    assert first.source_name == "rss_news"
    assert "Bitcoin ETF" in first.text
    # HTML tags from the description are stripped.
    assert "<p>" not in first.text and "<b>" not in first.text
    assert first.author_handle == "Jane Reporter"
    assert first.source_url == "https://example.com/btc-etf"
    assert first.author_type == "media"
    assert first.published_at == datetime(2026, 6, 9, 13, 45, tzinfo=timezone.utc)


def test_item_without_author_falls_back_to_feed_title():
    items = parse_feed(RSS_2_0, "rss_news")
    assert items[1].author_handle == "CryptoWire"


def test_parses_atom_entries():
    items = parse_feed(ATOM, "rss_news")
    assert len(items) == 1
    e = items[0]
    assert "Solana" in e.text
    assert e.source_url == "https://example.com/sol"
    assert e.author_handle == "Atom Author"
    assert e.published_at == datetime(2026, 6, 9, 10, 30, tzinfo=timezone.utc)


def test_malformed_xml_returns_empty_not_raise():
    assert parse_feed(b"<not valid xml", "rss_news") == []


def test_strip_html_collapses_whitespace():
    assert _strip_html("<p>hello   <b>world</b></p>") == "hello world"


def test_parse_date_handles_rfc822_and_iso():
    assert _parse_date("Mon, 09 Jun 2026 13:45:00 +0000") == datetime(2026, 6, 9, 13, 45, tzinfo=timezone.utc)
    assert _parse_date("2026-06-09T10:30:00Z") == datetime(2026, 6, 9, 10, 30, tzinfo=timezone.utc)
    assert _parse_date("garbage") is None
    assert _parse_date(None) is None


def test_source_name_is_never_mock():
    # The API filters evidence with NOT ILIKE 'mock%'; a real source must pass.
    c = RSSCollector(db_pool=None, feed_urls=["https://example.com/feed"])
    assert not c.source_name.lower().startswith("mock")
    assert c.source_name == "rss_news"


def test_politeness_gate_blocks_rapid_refetch():
    c = RSSCollector(db_pool=None, feed_urls=["https://x/feed"], poll_seconds=120)
    assert c._due() is True       # first call fires
    assert c._due() is False      # immediately after → throttled
