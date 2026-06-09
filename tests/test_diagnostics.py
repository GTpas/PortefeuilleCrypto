"""
Offline tests for the diagnostic CLIs (pure logic only — no HTTP/DB).

- diagnose_universe.summarize(): correct cause attribution for the common
  shortfall scenarios (full / capped / low-volume floor / REST down).
- benchmark_snapshot_api.percentile()/summarize_latencies(): correct stats.
"""

from scripts.diagnose_universe import summarize
from scripts.benchmark_snapshot_api import percentile, summarize_latencies


def _debug(**over):
    base = {
        "requested_limit": 300,
        "raw_binance_tickers_count": 2500,
        "quote_mismatch_count": 100,
        "eligible_symbols_count": 305,
        "excluded_not_spot_count": 0,
        "excluded_inactive_count": 0,
        "excluded_stable_count": 10,
        "excluded_leverage_count": 20,
        "excluded_low_volume_count": 1800,
        "final_universe_count": 300,
        "capped_by_limit": 5,
        "last_error": None,
    }
    base.update(over)
    return base


# ── diagnose_universe.summarize ──────────────────────────────

def test_universe_full_no_shortfall():
    s = summarize(_debug())
    assert s["loaded"] == 300
    assert s["missing_vs_requested"] == 0
    assert s["cause"] == "none"


def test_capped_when_eligible_exceeds_request_but_loaded_short():
    s = summarize(_debug(final_universe_count=250, capped_by_limit=55), requested=300)
    assert s["cause"] == "capped_by_limit"
    assert s["missing_vs_requested"] == 50


def test_low_volume_floor_is_dominant_cause():
    s = summarize(_debug(eligible_symbols_count=66, final_universe_count=66, capped_by_limit=0))
    assert s["loaded"] == 66
    assert s["cause"] == "low_volume_floor"          # the old "66" symptom
    assert "MIN_QUOTE_VOLUME" in s["recommendation"]


def test_rest_unavailable_when_no_raw_tickers():
    s = summarize(_debug(raw_binance_tickers_count=0, eligible_symbols_count=0,
                         final_universe_count=0, last_error="timeout"))
    assert s["cause"] == "rest_unavailable"
    assert s["last_error"] == "timeout"


def test_exclusions_partition_sums():
    s = summarize(_debug())
    assert s["exclusions_total"] == 10 + 20 + 1800
    assert {e["reason"] for e in s["exclusions"]} == {
        "not_spot", "inactive", "stable", "leverage", "low_volume"
    }


def test_requested_override_takes_precedence():
    s = summarize(_debug(), requested=150)
    assert s["requested"] == 150
    assert s["cause"] == "none"  # loaded 300 >= 150


# ── benchmark percentile / summary ───────────────────────────

def test_percentile_basic():
    data = [10, 20, 30, 40, 50]
    assert percentile(data, 50) == 30
    assert percentile(data, 0) == 10
    assert percentile(data, 100) == 50


def test_percentile_interpolates():
    assert percentile([0, 10], 50) == 5.0


def test_percentile_empty_and_single():
    assert percentile([], 95) == 0.0
    assert percentile([42.0], 95) == 42.0


def test_summarize_latencies_shape():
    s = summarize_latencies([5, 10, 15, 20])
    assert s["n"] == 4
    assert s["min"] == 5 and s["max"] == 20
    assert s["p50"] == percentile([5, 10, 15, 20], 50)
    assert s["avg"] == 12.5


def test_summarize_latencies_empty():
    s = summarize_latencies([])
    assert s["n"] == 0 and s["p95"] == 0.0
