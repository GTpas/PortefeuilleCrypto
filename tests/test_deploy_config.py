"""
Offline tests for deploy/config hardening (no network, no Docker daemon).

Verify the memory/log hardening is actually present:
  * .dockerignore exists and excludes heavy/secret paths from the build context
  * docker-compose.yml caps memory + rotates logs + keeps healthchecks
  * config exposes every new universe / tier / range / memory-bound setting
"""

import os

from config import settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


# ── .dockerignore ────────────────────────────────────────────────────────────

def test_dockerignore_present_and_excludes_heavy_paths():
    assert os.path.isfile(os.path.join(ROOT, ".dockerignore"))
    s = _read(".dockerignore")
    for needle in ("venv/", "__pycache__/", ".git/", ".pytest_cache/",
                   "node_modules/", ".env", "logs/"):
        assert needle in s, needle


# ── docker-compose hardening ─────────────────────────────────────────────────

def test_compose_has_log_rotation_mem_limits_healthchecks():
    s = _read("docker-compose.yml")
    assert "max-size" in s and "max-file" in s      # log rotation
    assert "mem_limit" in s and "memswap_limit" in s  # bounded memory
    assert "maxmemory" in s                           # redis hard cap
    assert "healthcheck" in s                          # liveness
    assert "restart: unless-stopped" in s


# ── config: new settings exist with sane defaults ────────────────────────────

def test_universe_settings():
    assert settings.UNIVERSE_LIMIT == 300
    assert settings.QUOTE_ASSET == "USDT"
    assert settings.MIN_QUOTE_VOLUME > 0
    assert settings.EXCLUDE_STABLES is True
    assert settings.EXCLUDE_LEVERAGE is True
    assert settings.TRENDING_REFRESH_SECONDS == 60
    assert settings.ENABLE_MARKET_UNIVERSE is True


def test_backend_memory_bound_settings():
    assert settings.BACKEND_MAX_SYMBOLS == 300
    assert settings.BACKEND_ACTIVE_SYMBOL_LIMIT == 20
    assert settings.MAX_CANDLES_BACKEND >= 500
    assert settings.MAX_MARKET_EVENTS > 0
    assert settings.BROADCAST_THROTTLE_MS > 0
    assert settings.SNAPSHOT_INTERVAL_SECONDS > 0
    assert settings.ENABLE_DEPTH_ONLY_FOR_SELECTED is True


def test_chart_range_settings():
    assert settings.CHART_RANGE_DEFAULT in ("1D", "7D", "1M", "1Y")
    assert settings.CHART_INTERVAL_1D and settings.CHART_INTERVAL_7D
    assert settings.CHART_INTERVAL_1M and settings.CHART_INTERVAL_1Y


def test_frontend_limit_settings():
    assert settings.MAX_CANDLES_PER_SYMBOL >= 500
    assert settings.MAX_VISIBLE_SYMBOLS > 0
    assert settings.MAX_EVENT_BUFFER > 0
    assert settings.MAX_LOG_BUFFER > 0
    assert settings.UI_UPDATE_THROTTLE_MS > 0
