"""
Dev Supervisor entrypoint
-------------------------
One command to bring up the whole local stack and supervise it:

    python scripts/dev_supervisor.py

It builds the process list from the files that ACTUALLY exist (never assumes a
worker is present), launches docker compose + bootstrap as one-shots, then the
long-running workers and the API, and serves an Ops API (real process state,
live logs, controlled start/stop/restart) at:

    http://127.0.0.1:8050/api/ops/status
    ws://127.0.0.1:8050/ws/ops

The cockpit's "Ops / Terminals" panel talks to this server. No raw shell is
exposed — only the controlled actions below.
"""

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import settings
from workers.process_supervisor import ProcessSupervisor, ProcessSpec

PY = sys.executable


def _exists(*parts: str) -> bool:
    return os.path.isfile(os.path.join(ROOT, *parts))


def build_specs() -> list[ProcessSpec]:
    """Only include a process if its file/compose actually exists on disk."""
    specs: list[ProcessSpec] = []

    if _exists("docker-compose.yml") or _exists("docker-compose.yaml"):
        specs.append(ProcessSpec("docker", ["docker", "compose", "up", "-d"],
                                  cwd=ROOT, optional=True, autorestart=False, oneshot=True))

    # bootstrap initializes schema/portfolio — run once before the workers.
    if _exists("workers", "bootstrap.py"):
        specs.append(ProcessSpec("bootstrap", [PY, "-m", "workers.bootstrap"],
                                  cwd=ROOT, autorestart=False, oneshot=True))

    long_running = [
        ("ingestor", "ingestor.py"),
        ("aggregator", "aggregator.py"),
        ("feature_worker", "feature_worker.py"),
        ("social_ingestor", "social_ingestor.py"),
        ("antigravity_bot", "antigravity_bot.py"),
        ("outcome_evaluator", "outcome_evaluator.py"),
        ("report_worker", "report_worker.py"),
    ]
    for name, fname in long_running:
        if _exists("workers", fname):
            specs.append(ProcessSpec(name, [PY, "-m", f"workers.{name}"], cwd=ROOT))

    # API (serves the cockpit at :8000). Reload is omitted under supervision so
    # the PID we track is the real server, not a reloader parent.
    if _exists("api", "main.py"):
        specs.append(ProcessSpec(
            "api",
            [PY, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=ROOT,
        ))
    return specs


# ── Incident sink (Phase 9): persist + surface a structured summary ─────────

def _incident_sink(incident: dict) -> None:
    """Real, consumable incident channel. A Claude/webhook integration can tail
    this file or be wired here later — we never fabricate an incident."""
    line = json.dumps(incident, default=str)
    print(f"\n[INCIDENT {incident['severity'].upper()}] {incident['process']}: "
          f"{incident['suspected_root_cause']}", file=sys.stderr)
    try:
        logs_dir = os.path.join(ROOT, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        with open(os.path.join(logs_dir, "ops_incidents.jsonl"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Ops API ─────────────────────────────────────────────────────────────────

def build_app(sup: ProcessSupervisor) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(sup.start_all())
        yield
        task.cancel()
        await sup.stop_all()

    app = FastAPI(title="Antigravity Ops Supervisor", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/ops/status")
    async def ops_status():
        return sup.status()

    @app.get("/api/ops/health")
    async def ops_health():
        st = sup.status()
        return {"status": st["status"], "running": st["running"], "total": st["total"],
                "degraded": st["degraded"]}

    @app.get("/api/ops/processes")
    async def ops_processes():
        return sup.processes()

    @app.get("/api/ops/events")
    async def ops_events(limit: int = 200, level: str = None, process: str = None):
        return sup.get_events(limit=limit, level=level, process=process)

    @app.get("/api/ops/incidents")
    async def ops_incidents(limit: int = 50):
        return list(sup.incidents)[-limit:]

    @app.post("/api/ops/process/{action}")
    async def ops_process_action(action: str, request: Request):
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        name = body.get("name") or request.query_params.get("name")
        if not name:
            return {"ok": False, "error": "missing 'name'"}
        return await sup.control(name, action)

    @app.post("/api/ops/frontend-error")
    async def ops_frontend_error(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        sup.emit_frontend_error(payload)
        return {"ok": True}

    @app.websocket("/ws/ops")
    async def ws_ops(ws: WebSocket):
        await ws.accept()
        q = sup.subscribe()
        try:
            # Prime with a status snapshot so the UI renders immediately.
            await ws.send_text(json.dumps({"type": "snapshot", "ts": time.time(),
                                           "status": sup.status()}))
            while True:
                event = await q.get()
                await ws.send_text(json.dumps(event, default=str))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            sup.unsubscribe(q)

    return app


async def main():
    specs = build_specs()
    if not specs:
        print("No known process files found — nothing to supervise.", file=sys.stderr)
        return

    sup = ProcessSupervisor(
        specs,
        max_restarts=settings.OPS_MAX_RESTARTS,
        restart_window_s=settings.OPS_RESTART_WINDOW_S,
    )
    sup.on_incident = _incident_sink

    print("Supervising:", ", ".join(s.name for s in specs))
    print(f"Ops API:  http://{settings.OPS_HOST}:{settings.OPS_PORT}/api/ops/status")
    print(f"Cockpit:  http://127.0.0.1:8000/  (Ops panel reads the Ops API above)")

    app = build_app(sup)
    config = uvicorn.Config(app, host=settings.OPS_HOST, port=settings.OPS_PORT,
                            log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSupervisor stopped by user.")
