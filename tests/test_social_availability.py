"""
Anti-mock / real-data guarantees for the social signal path.

These tests pin the invariants behind the "no fabricated data" rule:
  1. With no real social content, S_social is reported UNAVAILABLE and neutral
     (0.0) — it must NOT fabricate the spurious bearish score the normalizers
     would otherwise produce (normalize(0, 0, 4) == -1).
  2. The simulated collector stays clearly named 'mock_social' so the API can
     filter it out of evidence/scores.
  3. The mock social collector is OFF by default — it must never run (and never
     reach the cockpit as "real") unless a developer explicitly opts in.
"""

import asyncio

import pytest

from signal_engine.social_engine import SocialEngine


class _FakeAcquire:
    """Async-context-manager wrapper returned by FakePool.acquire()."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _NoContentConn:
    """Connection stub whose availability count is always zero."""

    async def fetchval(self, *args, **kwargs):
        return 0  # content_count == 0 -> unavailable short-circuit

    async def fetch(self, *args, **kwargs):  # pragma: no cover - safety net
        return []


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


def test_no_social_content_is_unavailable_not_fabricated():
    engine = SocialEngine(_FakePool(_NoContentConn()))
    result = asyncio.run(engine.compute_social_score("BTC/USDT"))

    assert result["available"] is False
    # Neutral, NOT the spurious negative the normalizers would produce.
    assert result["score"] == 0.0
    assert result["metrics"]["social_available"] is False
    assert result["factors"][0]["name"] == "social_unavailable"


def test_mock_collector_is_clearly_named_mock():
    from social.mock_collector import MockSocialCollector

    # source_name drives the API's `NOT ILIKE 'mock%'` evidence filter.
    assert MockSocialCollector(db_pool=None).source_name.startswith("mock")


def test_mock_social_disabled_by_default():
    from config import Settings

    # A fresh Settings with no env override must keep mock social OFF, so the
    # cockpit never presents simulated tweets/authors/scores as real.
    assert Settings(_env_file=None).ENABLE_MOCK_SOCIAL is False
