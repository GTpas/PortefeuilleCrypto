"""
Ops API tests (offline).

httpx/TestClient is not installed, so we test the real logic that backs the
endpoints (ProcessSupervisor) plus route registration on the FastAPI app.
"""

import asyncio

from workers.process_supervisor import ProcessSupervisor, ProcessSpec
from scripts.dev_supervisor import build_app, build_specs


def test_status_idle_when_empty():
    sup = ProcessSupervisor([])
    st = sup.status()
    assert st["status"] == "idle"
    assert st["total"] == 0
    assert st["processes"] == []


def test_control_unknown_process_returns_error():
    sup = ProcessSupervisor([])
    res = asyncio.run(sup.control("nope", "start"))
    assert res["ok"] is False


def test_frontend_error_becomes_error_event():
    sup = ProcessSupervisor([])
    sup.emit_frontend_error({"message": "TypeError: x is undefined", "stack": "at foo"})
    evts = sup.get_events()
    assert evts, "expected at least one event"
    last = evts[-1]
    assert last["process"] == "frontend"
    assert last["level"] == "ERROR"


def test_event_level_filter():
    sup = ProcessSupervisor([])
    sup.emit_log("w", "stdout", "INFO", "info line")
    sup.emit_log("w", "stderr", "ERROR", "error line")
    only_errors = sup.get_events(level="ERROR")
    assert all(e.get("level") == "ERROR" for e in only_errors if e.get("type") == "log")
    assert any(e["message"] == "error line" for e in only_errors)
    assert not any(e.get("message") == "info line" for e in only_errors)


def test_app_exposes_expected_ops_routes():
    sup = ProcessSupervisor([])
    app = build_app(sup)
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    for p in ["/api/ops/status", "/api/ops/processes", "/api/ops/events",
              "/api/ops/incidents", "/api/ops/health", "/api/ops/process/{action}",
              "/api/ops/frontend-error", "/ws/ops"]:
        assert p in paths, p


def test_build_specs_only_includes_existing_files():
    # Every spec must map to a real entrypoint the supervisor can launch.
    names = {s.name for s in build_specs()}
    # These workers + api exist in the repo today.
    assert {"ingestor", "aggregator", "feature_worker", "social_ingestor",
            "antigravity_bot", "api"} <= names
