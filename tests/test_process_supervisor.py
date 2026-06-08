"""
Process supervisor tests (Phase 13).

These spawn REAL short-lived Python subprocesses (no mocks) to verify the
supervisor actually captures stdout/stderr, classifies levels, accumulates
tracebacks, and detects crashes with a structured incident.
"""

import asyncio
import sys
import time

import pytest

from workers.process_supervisor import (
    ProcessSupervisor, ProcessSpec, detect_level,
)

PY = sys.executable


def _spec(name, code, **kw):
    return ProcessSpec(name, [PY, "-u", "-c", code], **kw)


async def _await_terminal(mp, timeout=10.0):
    start = time.time()
    while mp.status in ("pending", "starting", "running"):
        if time.time() - start > timeout:
            raise AssertionError(f"{mp.spec.name} did not terminate (status={mp.status})")
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)  # let stream readers flush to EOF


def test_detect_level_pure():
    assert detect_level("2026 - eng - ERROR - boom", "stdout") == "ERROR"
    assert detect_level("plain message", "stdout") == "INFO"
    assert detect_level("some warning text", "stderr") == "WARNING"
    assert detect_level("ValueError: nope", "stderr") == "ERROR"
    assert detect_level("2026 - x - CRITICAL - db down", "stderr") == "CRITICAL"


def test_captures_stdout_and_stderr():
    async def run():
        sup = ProcessSupervisor([_spec(
            "echo",
            "import sys; print('hello-out'); print('ERROR oops', file=sys.stderr)",
            oneshot=True, autorestart=False,
        )])
        mp = sup.procs["echo"]
        await sup.start("echo")
        await _await_terminal(mp)
        msgs = [e["message"] for e in mp.recent_logs]
        assert any("hello-out" in m for m in msgs), msgs
        assert any("oops" in m for m in msgs), msgs
        assert mp.status == "completed"
        assert mp.exit_code == 0
    asyncio.run(run())


def test_detects_crash_and_raises_incident():
    async def run():
        sup = ProcessSupervisor([_spec(
            "crasher", "import sys; sys.exit(3)", autorestart=False,
        )])
        mp = sup.procs["crasher"]
        await sup.start("crasher")
        await _await_terminal(mp)
        assert mp.status == "crashed"
        assert mp.exit_code == 3
        assert len(sup.incidents) >= 1
        assert sup.incidents[-1]["process"] == "crasher"
    asyncio.run(run())


def test_captures_python_traceback():
    async def run():
        sup = ProcessSupervisor([_spec(
            "boom", "raise ValueError('kaboom')", autorestart=False,
        )])
        mp = sup.procs["boom"]
        await sup.start("boom")
        await _await_terminal(mp)
        assert mp.last_traceback is not None
        assert "Traceback (most recent call last):" in mp.last_traceback
        assert "ValueError" in (mp.last_traceback or "")
    asyncio.run(run())


def test_control_unknown_process():
    async def run():
        sup = ProcessSupervisor([])
        res = await sup.control("ghost", "start")
        assert res["ok"] is False
    asyncio.run(run())
