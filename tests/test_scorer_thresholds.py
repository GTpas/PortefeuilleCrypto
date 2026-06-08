"""
Regression tests for the decision threshold mapping.

The key invariant (PR1): a neutral composite score (S_total ≈ 0) must map to
HOLD. The previous mapping exited below 0.15, which liquidated positions on
absence of signal. Thresholds are symmetric around 0 since S_total ∈ [-1, +1].
"""

import pytest

from signal_engine.scorer import (
    SignalEngine,
    REINFORCE_THRESHOLD,
    BUY_THRESHOLD,
    REDUCE_THRESHOLD,
    EXIT_THRESHOLD,
)

decide = SignalEngine._decide_action


@pytest.mark.parametrize("s_total", [0.0, 0.1, -0.1, 0.29, -0.29])
def test_neutral_scores_hold(s_total):
    """A neutral / weak score must HOLD — never liquidate on absence of signal."""
    action, reason = decide(s_total, [])
    assert action == "hold"
    assert reason == "hold_neutral"


def test_buy_threshold():
    assert decide(BUY_THRESHOLD, [])[0] == "buy"
    assert decide(0.5, [])[0] == "buy"


def test_reinforce_threshold():
    assert decide(REINFORCE_THRESHOLD, [])[0] == "reinforce"
    assert decide(0.95, [])[0] == "reinforce"


def test_reduce_threshold():
    assert decide(REDUCE_THRESHOLD, [])[0] == "reduce"
    assert decide(-0.5, [])[0] == "reduce"


def test_exit_threshold():
    assert decide(EXIT_THRESHOLD, [])[0] == "exit"
    assert decide(-0.9, [])[0] == "exit"


def test_thresholds_are_symmetric_and_ordered():
    assert EXIT_THRESHOLD < REDUCE_THRESHOLD < 0 < BUY_THRESHOLD < REINFORCE_THRESHOLD


def test_risk_gate_forces_hold_regardless_of_score():
    """Even a maximally bullish score must HOLD when a risk gate is triggered."""
    action, reason = decide(1.0, ["data_stale (45.0s > 30s)"])
    assert action == "hold"
    assert reason.startswith("risk_gate:")
