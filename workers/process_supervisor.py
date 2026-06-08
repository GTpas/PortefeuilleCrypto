"""
Process Supervisor (local dev)
------------------------------
Owns the lifecycle of the project's child processes (docker compose, workers,
the API) on a single local machine. It is the single source of truth for
process state, captures stdout/stderr line-by-line, classifies log levels,
accumulates Python tracebacks, auto-restarts crashed processes with exponential
backoff (capped by a sliding restart budget), and emits structured incidents.

This module is intentionally transport-agnostic: it exposes plain Python state
and an async event stream. `scripts/dev_supervisor.py` wires it to an HTTP/WS
Ops API. Everything here is real — no mock processes, no fabricated status.

Designed to run under `asyncio.run` (Windows Proactor loop supports subprocess).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

# ── Log level / traceback detection ─────────────────────────────────────────

_LEVEL_RE = re.compile(r"\b(CRITICAL|FATAL|ERROR|WARNING|WARN|INFO|DEBUG)\b")
_TRACEBACK_START = "Traceback (most recent call last):"
# A Python exception summary line, e.g. "ValueError: ...", "asyncpg.Error: ...".
_EXC_SUMMARY_RE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning|Interrupt|Exit|Timeout)\b")

_LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "WARN": 2, "ERROR": 3, "CRITICAL": 4, "FATAL": 4}


def detect_level(line: str, stream: str) -> str:
    """Classify a log line into INFO/WARNING/ERROR/CRITICAL/DEBUG.

    Honest heuristic: prefer an explicit level token; otherwise treat
    exception-looking stderr as ERROR and the rest as stream default.
    """
    m = _LEVEL_RE.search(line)
    if m:
        tok = m.group(1).upper()
        return "WARNING" if tok == "WARN" else ("CRITICAL" if tok == "FATAL" else tok)
    if stream == "stderr":
        if line.startswith(_TRACEBACK_START) or _EXC_SUMMARY_RE.match(line.strip()):
            return "ERROR"
        return "WARNING"
    return "INFO"


# ── Specs & state ───────────────────────────────────────────────────────────

@dataclass
class ProcessSpec:
    name: str
    argv: List[str]
    cwd: Optional[str] = None
    optional: bool = False          # absent file → skipped, not an error
    autorestart: bool = True
    oneshot: bool = False           # run to completion once (e.g. docker compose up -d)


@dataclass
class ManagedProcess:
    spec: ProcessSpec
    status: str = "pending"         # pending|starting|running|stopped|crashed|degraded|completed
    pid: Optional[int] = None
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    exit_code: Optional[int] = None
    restarts: int = 0
    last_log: Optional[str] = None
    last_log_level: Optional[str] = None
    last_traceback: Optional[str] = None
    last_seen_at: Optional[float] = None
    heartbeat_at: Optional[float] = None
    recent_logs: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    # internal
    _proc: Optional[asyncio.subprocess.Process] = None
    _stop_requested: bool = False
    _restart_times: Deque[float] = field(default_factory=lambda: deque(maxlen=50))
    _tb_buffer: List[str] = field(default_factory=list)
    _collecting_tb: bool = False

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        uptime = (now - self.started_at) if (self.status == "running" and self.started_at) else None
        return {
            "name": self.spec.name,
            "status": self.status,
            "pid": self.pid,
            "optional": self.spec.optional,
            "oneshot": self.spec.oneshot,
            "autorestart": self.spec.autorestart,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "uptime_s": round(uptime, 1) if uptime is not None else None,
            "exit_code": self.exit_code,
            "restarts": self.restarts,
            "last_log": self.last_log,
            "last_log_level": self.last_log_level,
            "last_traceback": self.last_traceback,
            "last_seen_at": self.last_seen_at,
            "heartbeat_at": self.heartbeat_at,
        }


# ── Supervisor ──────────────────────────────────────────────────────────────

class ProcessSupervisor:
    def __init__(
        self,
        specs: List[ProcessSpec],
        max_restarts: int = 5,
        restart_window_s: int = 120,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 30.0,
    ):
        self.procs: Dict[str, ManagedProcess] = {s.name: ManagedProcess(spec=s) for s in specs}
        self.max_restarts = max_restarts
        self.restart_window_s = restart_window_s
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s

        self.events: Deque[Dict[str, Any]] = deque(maxlen=1000)
        self.incidents: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._subscribers: List["asyncio.Queue[Dict[str, Any]]"] = []
        self._tasks: List[asyncio.Task] = []
        self._event_seq = 0
        self._incident_seq = 0
        # Optional hook: called with each structured incident (e.g. notify Claude).
        self.on_incident: Optional[Callable[[Dict[str, Any]], None]] = None

    # ── Event bus ──
    def subscribe(self) -> "asyncio.Queue[Dict[str, Any]]":
        q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=500)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[Dict[str, Any]]") -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _emit(self, event: Dict[str, Any]) -> None:
        self._event_seq += 1
        event.setdefault("ts", time.time())
        event["seq"] = self._event_seq
        self.events.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest for slow consumers rather than blocking the supervisor.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    def emit_log(self, name: str, stream: str, level: str, message: str,
                 traceback: Optional[str] = None) -> None:
        self._emit({
            "type": "log", "process": name, "stream": stream,
            "level": level, "message": message, "traceback": traceback,
        })

    def emit_status(self, name: str) -> None:
        mp = self.procs.get(name)
        if mp:
            self._emit({"type": "status", "process": name, "snapshot": mp.snapshot()})

    def emit_frontend_error(self, payload: Dict[str, Any]) -> None:
        """Phase 10: POST /api/ops/frontend-error funnels browser errors here."""
        msg = str(payload.get("message", ""))[:1000]
        self.emit_log("frontend", "stderr", "ERROR", f"[frontend] {msg}",
                      traceback=payload.get("stack"))

    # ── Incident (Phase 9 structured summary) ──
    def _raise_incident(self, mp: ManagedProcess, severity: str,
                        suspected_root_cause: str, recommended_action: str) -> Dict[str, Any]:
        self._incident_seq += 1
        error_type = None
        if mp.last_traceback:
            last_line = mp.last_traceback.strip().splitlines()[-1] if mp.last_traceback.strip() else ""
            error_type = last_line.split(":", 1)[0] if last_line else None
        incident = {
            "incident_id": f"inc-{mp.spec.name}-{self._incident_seq}-{int(mp.last_seen_at or time.time())}",
            "severity": severity,                    # warning|error|critical
            "process": mp.spec.name,
            "symbol": None,
            "started_at": mp.started_at,
            "last_seen_at": mp.last_seen_at,
            "error_type": error_type,
            "exit_code": mp.exit_code,
            "traceback": mp.last_traceback,
            "recent_logs": list(mp.recent_logs)[-20:],
            "health_status": {"status": mp.status, "restarts": mp.restarts},
            "market_data_freshness": None,           # filled by the API layer if available
            "suspected_root_cause": suspected_root_cause,
            "recommended_action": recommended_action,
        }
        self.incidents.append(incident)
        self._emit({"type": "incident", "process": mp.spec.name,
                    "severity": severity, "incident": incident})
        if self.on_incident:
            try:
                self.on_incident(incident)
            except Exception:
                pass
        return incident

    # ── Process lifecycle ──
    async def start(self, name: str) -> bool:
        mp = self.procs[name]
        if mp._proc is not None and mp.status in ("running", "starting"):
            return False
        mp._stop_requested = False
        mp.status = "starting"
        mp.exit_code = None
        self.emit_status(name)
        try:
            proc = await asyncio.create_subprocess_exec(
                *mp.spec.argv,
                cwd=mp.spec.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            mp.status = "crashed"
            mp.last_log = f"failed to spawn: {e}"
            mp.last_log_level = "CRITICAL"
            mp.last_seen_at = time.time()
            self.emit_log(name, "stderr", "CRITICAL", mp.last_log)
            self.emit_status(name)
            self._raise_incident(mp, "critical", f"spawn failed: {e}",
                                 "Check the command/path and that dependencies are installed.")
            return False

        mp._proc = proc
        mp.pid = proc.pid
        mp.started_at = time.time()
        mp.last_seen_at = mp.started_at
        mp.status = "running"
        self.emit_status(name)
        self.emit_log(name, "stdout", "INFO", f"started pid={proc.pid}: {' '.join(mp.spec.argv)}")

        self._tasks.append(asyncio.create_task(self._read_stream(mp, proc.stdout, "stdout")))
        self._tasks.append(asyncio.create_task(self._read_stream(mp, proc.stderr, "stderr")))
        self._tasks.append(asyncio.create_task(self._monitor(mp)))
        return True

    async def _read_stream(self, mp: ManagedProcess, stream: Optional[asyncio.StreamReader],
                           which: str) -> None:
        if stream is None:
            return
        while True:
            try:
                raw = await stream.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            self._handle_line(mp, which, line)

    def _handle_line(self, mp: ManagedProcess, stream: str, line: str) -> None:
        now = time.time()
        mp.last_seen_at = now
        mp.heartbeat_at = now

        # Traceback accumulation: collect from the header until the exception
        # summary line, then flush as one structured traceback.
        if line.startswith(_TRACEBACK_START):
            mp._collecting_tb = True
            mp._tb_buffer = [line]
            return
        if mp._collecting_tb:
            mp._tb_buffer.append(line)
            # End of traceback = a non-indented exception summary line.
            if not line.startswith((" ", "\t")) and (
                _EXC_SUMMARY_RE.match(line) or _LEVEL_RE.search(line) is None and ":" in line
            ):
                tb = "\n".join(mp._tb_buffer)
                mp.last_traceback = tb
                mp._collecting_tb = False
                mp._tb_buffer = []
                mp.last_log = line
                mp.last_log_level = "ERROR"
                evt = {"ts": now, "stream": stream, "level": "ERROR", "message": line}
                mp.recent_logs.append(evt)
                self.emit_log(mp.spec.name, stream, "ERROR", line, traceback=tb)
            return

        level = detect_level(line, stream)
        mp.last_log = line
        mp.last_log_level = level
        evt = {"ts": now, "stream": stream, "level": level, "message": line}
        mp.recent_logs.append(evt)
        self.emit_log(mp.spec.name, stream, level, line)

    async def _monitor(self, mp: ManagedProcess) -> None:
        proc = mp._proc
        assert proc is not None
        exit_code = await proc.wait()
        mp.exit_code = exit_code
        mp.stopped_at = time.time()
        mp.last_seen_at = mp.stopped_at
        mp.pid = None
        mp._proc = None

        if mp.spec.oneshot and exit_code == 0:
            mp.status = "completed"
            self.emit_log(mp.spec.name, "stdout", "INFO", "completed (exit 0)")
            self.emit_status(mp.spec.name)
            return

        if mp._stop_requested:
            mp.status = "stopped"
            self.emit_log(mp.spec.name, "stdout", "INFO", "stopped by request")
            self.emit_status(mp.spec.name)
            return

        # Unexpected exit → crashed.
        clean = exit_code == 0
        mp.status = "crashed"
        level = "WARNING" if clean else "ERROR"
        self.emit_log(mp.spec.name, "stderr", level, f"exited unexpectedly (code {exit_code})")
        self.emit_status(mp.spec.name)

        if not mp.spec.autorestart:
            self._raise_incident(
                mp, "error" if not clean else "warning",
                f"{mp.spec.name} exited (code {exit_code}), autorestart disabled",
                "Inspect the traceback/logs and restart manually from the Ops panel.",
            )
            return

        # Sliding restart budget.
        now = time.time()
        mp._restart_times.append(now)
        window_count = sum(1 for t in mp._restart_times if now - t <= self.restart_window_s)
        if window_count > self.max_restarts:
            mp.status = "degraded"
            self.emit_status(mp.spec.name)
            self._raise_incident(
                mp, "critical",
                f"{mp.spec.name} crashed {window_count}x within {self.restart_window_s}s",
                "Crash loop — stop auto-restart, fix the root cause (see traceback) before resuming.",
            )
            return

        # Exponential backoff before restart.
        delay = min(self.backoff_cap_s, self.backoff_base_s * (2 ** mp.restarts))
        mp.restarts += 1
        self.emit_log(mp.spec.name, "stdout", "WARNING",
                      f"auto-restart #{mp.restarts} in {delay:.0f}s")
        self._raise_incident(
            mp, "error" if not clean else "warning",
            f"{mp.spec.name} crashed (code {exit_code}); auto-restarting",
            f"Auto-restart #{mp.restarts} scheduled in {delay:.0f}s; watch for a crash loop.",
        )
        await asyncio.sleep(delay)
        if not mp._stop_requested:
            await self.start(mp.spec.name)

    async def stop(self, name: str, timeout: float = 8.0) -> bool:
        mp = self.procs.get(name)
        if not mp or mp._proc is None:
            return False
        mp._stop_requested = True
        proc = mp._proc
        try:
            proc.terminate()
        except ProcessLookupError:
            return True
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
        return True

    async def restart(self, name: str) -> bool:
        mp = self.procs.get(name)
        if not mp:
            return False
        if mp._proc is not None:
            await self.stop(name)
        mp.restarts = 0
        mp._restart_times.clear()
        return await self.start(name)

    async def control(self, name: str, action: str) -> Dict[str, Any]:
        if name not in self.procs:
            return {"ok": False, "error": f"unknown process: {name}"}
        if action == "start":
            ok = await self.start(name)
        elif action == "stop":
            ok = await self.stop(name)
        elif action == "restart":
            ok = await self.restart(name)
        else:
            return {"ok": False, "error": f"unknown action: {action}"}
        return {"ok": ok, "process": name, "action": action,
                "snapshot": self.procs[name].snapshot()}

    async def start_all(self) -> None:
        """Start every spec in order. Oneshots are awaited to completion first."""
        for mp in self.procs.values():
            if mp.spec.oneshot:
                await self.start(mp.spec.name)
                # Wait for the oneshot (e.g. docker compose up -d) to finish.
                while mp.status == "running":
                    await asyncio.sleep(0.2)
            else:
                await self.start(mp.spec.name)

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(n) for n in self.procs), return_exceptions=True)

    # ── Read APIs ──
    def status(self) -> Dict[str, Any]:
        snaps = [mp.snapshot() for mp in self.procs.values()]
        running = sum(1 for s in snaps if s["status"] == "running")
        degraded = [s["name"] for s in snaps if s["status"] in ("degraded", "crashed")]
        overall = "degraded" if degraded else ("ok" if running else "idle")
        return {"status": overall, "running": running, "total": len(snaps),
                "degraded": degraded, "processes": snaps}

    def processes(self) -> List[Dict[str, Any]]:
        return [mp.snapshot() for mp in self.procs.values()]

    def get_events(self, limit: int = 200, level: Optional[str] = None,
                   process: Optional[str] = None) -> List[Dict[str, Any]]:
        items = list(self.events)
        if process:
            items = [e for e in items if e.get("process") == process]
        if level:
            min_rank = _LEVEL_RANK.get(level.upper(), 0)
            items = [e for e in items
                     if e.get("type") != "log" or _LEVEL_RANK.get((e.get("level") or "INFO").upper(), 0) >= min_rank]
        return items[-limit:]
