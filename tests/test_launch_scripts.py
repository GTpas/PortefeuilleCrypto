"""
Launch tooling tests (offline).

Verify the VS Code tasks and PowerShell launch scripts exist and are correct —
in particular that they use VALID PowerShell syntax for PYTHONPATH (the
';'-separated form) and never the invalid `$env:PYTHONPATH="." python ...`
form that throws "Jeton inattendu « python »".
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8-sig") as f:
        return f.read()


def test_vscode_tasks_present_and_valid_json():
    raw = _read(".vscode", "tasks.json")
    data = json.loads(raw)  # strict JSON must parse
    labels = {t["label"] for t in data["tasks"]}
    assert {"Start Dev Supervisor", "Start Full Stack", "Stop Full Stack"} <= labels


def test_tasks_use_powershell_and_valid_pythonpath():
    data = json.loads(_read(".vscode", "tasks.json"))
    # Global shell is PowerShell.
    assert "powershell" in data["options"]["shell"]["executable"].lower()
    commands = " \n ".join(t.get("command", "") for t in data["tasks"])
    # The valid, ';'-separated form must be present...
    assert '$env:PYTHONPATH="."' in commands and ";" in commands
    # ...and the invalid token-adjacent form must NOT appear.
    assert not re.search(r'\$env:PYTHONPATH="\."\s+python', commands)


def test_launch_scripts_exist():
    for name in ("start_dev_supervisor.ps1", "start_all.ps1", "stop_all.ps1"):
        assert os.path.isfile(os.path.join(ROOT, "scripts", name)), name


def test_start_supervisor_script_content():
    s = _read("scripts", "start_dev_supervisor.ps1")
    assert '$env:PYTHONPATH' in s
    assert "dev_supervisor.py" in s
    # Advertises the useful URLs.
    assert "8050" in s and "8000" in s
    # No invalid PowerShell token-adjacency.
    assert not re.search(r'\$env:PYTHONPATH\s*=\s*"\."\s+python', s)


def test_start_all_does_not_duplicate_processes():
    s = _read("scripts", "start_all.ps1")
    # Full-stack launcher must defer to the supervisor, not relaunch workers.
    assert "start_dev_supervisor.ps1" in s
    assert "8050" in s  # checks for an already-running supervisor
    assert "workers.ingestor" not in s and "uvicorn" not in s
