"""
Offline tests for the ex-post outcome evaluator's pure logic:
return computation, action-vs-outcome correctness, and horizon mapping.
No DB/network — only the module-level helpers.
"""

import pytest

from workers.outcome_evaluator import (
    return_pct, classify_correct, horizon_pg_interval,
)


def test_return_pct_basic():
    assert return_pct(100.0, 110.0) == pytest.approx(10.0)
    assert return_pct(100.0, 90.0) == pytest.approx(-10.0)
    assert return_pct(100.0, 100.0) == pytest.approx(0.0)


def test_return_pct_guards_invalid_base():
    assert return_pct(0.0, 100.0) is None
    assert return_pct(-5.0, 100.0) is None


def test_buy_correct_when_price_rises():
    assert classify_correct("buy", 2.0, 0.5) is True
    assert classify_correct("buy", -2.0, 0.5) is False
    assert classify_correct("reinforce", 0.1, 0.5) is True


def test_exit_correct_when_price_falls():
    assert classify_correct("exit", -3.0, 0.5) is True
    assert classify_correct("exit", 3.0, 0.5) is False
    assert classify_correct("reduce", -1.0, 0.5) is True


def test_hold_correct_within_band_only():
    assert classify_correct("hold", 0.3, 0.5) is True     # quiet → hold vindicated
    assert classify_correct("hold", -0.4, 0.5) is True
    assert classify_correct("hold", 1.2, 0.5) is False    # big move → hold was wrong
    assert classify_correct("hold", -1.2, 0.5) is False


def test_unknown_action_or_missing_return_is_none():
    assert classify_correct("buy", None, 0.5) is None
    assert classify_correct("weird", 5.0, 0.5) is None


def test_horizon_interval_mapping():
    assert horizon_pg_interval("1h") == "1 hour"
    assert horizon_pg_interval("4h") == "4 hours"
    assert horizon_pg_interval("24h") == "24 hours"
    assert horizon_pg_interval("15m") == "15 minutes"
    assert horizon_pg_interval("nope") is None
