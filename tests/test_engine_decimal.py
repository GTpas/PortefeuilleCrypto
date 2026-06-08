"""
Regression tests for the float/Decimal mix in the paper-trading engine.

Bug: `paper_execution/engine.py::get_portfolio_state` returned position rows as
raw `dict(record)`. asyncpg maps NUMERIC columns (`qty`, `average_entry_price`,
`unrealized_pnl`) to `decimal.Decimal`, while live prices arrive from the bot as
`float`. `update_portfolio_value` then computed `(price - average_entry_price)`,
raising `TypeError: unsupported operand type(s) for -: 'float' and 'decimal.Decimal'`
and crashing the bot loop (which only catches CancelledError).

Fix: normalize the NUMERIC columns to float at the boundary in
`get_portfolio_state`. These tests fail if that normalization is reverted.
"""

import asyncio
from decimal import Decimal

import pytest

from paper_execution.engine import PaperExecutionEngine


class _FakeAcquire:
    """Mimics `async with pool.acquire() as conn`."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Returns Decimal-typed rows like asyncpg does for NUMERIC columns."""

    def __init__(self, portfolio_row, position_rows, captured):
        self._portfolio_row = portfolio_row
        self._position_rows = position_rows
        self._captured = captured

    async def fetchrow(self, query, *args):
        return self._portfolio_row

    async def fetch(self, query, *args):
        return self._position_rows

    async def execute(self, query, *args):
        self._captured.append((query, args))


def _make_pool(portfolio_row, position_rows, captured):
    conn = _FakeConn(portfolio_row, position_rows, captured)
    pool = type("_FakePool", (), {"acquire": lambda self: _FakeAcquire(conn)})()
    return pool


def _decimal_state():
    """A portfolio + one position with every NUMERIC field as Decimal."""
    portfolio_row = {
        "id": 1,
        "initial_capital": Decimal("10000"),
        "current_cash": Decimal("5000"),
        "total_value": Decimal("10000"),
    }
    position_rows = [
        {
            "symbol": "BTC-USD",
            "exchange_code": "coinbase",
            "qty": Decimal("0.5"),
            "average_entry_price": Decimal("60000"),
            "unrealized_pnl": Decimal("0"),
        }
    ]
    return portfolio_row, position_rows


def test_get_portfolio_state_normalizes_numeric_to_float():
    """NUMERIC columns must cross the boundary as float, not Decimal."""
    portfolio_row, position_rows = _decimal_state()
    engine = PaperExecutionEngine(_make_pool(portfolio_row, position_rows, []))

    state = asyncio.run(engine.get_portfolio_state())
    pos = state["positions"][0]

    assert isinstance(pos["qty"], float)
    assert isinstance(pos["average_entry_price"], float)
    assert isinstance(pos["unrealized_pnl"], float)
    assert isinstance(state["current_cash"], float)


def test_update_portfolio_value_no_decimal_typeerror():
    """The original crash: float price minus Decimal entry price must not raise,
    and must compute the correct unrealized PnL and total value as floats."""
    portfolio_row, position_rows = _decimal_state()
    captured = []
    engine = PaperExecutionEngine(_make_pool(portfolio_row, position_rows, captured))

    # Price arrives from the bot as a plain float (current_prices: Dict[str, float]).
    asyncio.run(engine.update_portfolio_value({"BTC-USD": 65000.0}))

    # Two UPDATEs: paper_position.unrealized_pnl, then paper_portfolio.total_value.
    pnl_arg = captured[0][1][0]
    total_value_arg = captured[-1][1][0]

    # unrealized_pnl = (65000 - 60000) * 0.5 = 2500
    assert isinstance(pnl_arg, float)
    assert pnl_arg == pytest.approx(2500.0)
    # total_value = cash 5000 + qty 0.5 * price 65000 = 37500
    assert isinstance(total_value_arg, float)
    assert total_value_arg == pytest.approx(37500.0)


def test_update_portfolio_value_skips_unpriced_positions():
    """A position with no current price is left untouched (no UPDATE, no crash)."""
    portfolio_row, position_rows = _decimal_state()
    captured = []
    engine = PaperExecutionEngine(_make_pool(portfolio_row, position_rows, captured))

    asyncio.run(engine.update_portfolio_value({}))  # no price for BTC-USD

    # Only the final paper_portfolio total_value UPDATE runs; total = cash only.
    assert len(captured) == 1
    assert captured[0][1][0] == pytest.approx(5000.0)
