# ============================================================
# Start Dev Supervisor (Windows PowerShell)
# Launches the local process supervisor, which itself starts
# docker compose, bootstrap, the workers and the API.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_dev_supervisor.ps1
# ============================================================

$ErrorActionPreference = 'Stop'

# 1. Move to project root (parent of this scripts/ folder).
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Write-Host "[supervisor] project root: $root" -ForegroundColor DarkGray

# 2. Resolve a Python interpreter (prefer the project venv).
$py = $null
$venvPy = Join-Path $root 'venv\Scripts\python.exe'
$dotVenvPy = Join-Path $root '.venv\Scripts\python.exe'
$activate = Join-Path $root 'venv\Scripts\Activate.ps1'

if (Test-Path $venvPy) {
    $py = $venvPy
    if (Test-Path $activate) { try { & $activate } catch {} }
    Write-Host "[supervisor] using venv: $py" -ForegroundColor DarkGray
} elseif (Test-Path $dotVenvPy) {
    $py = $dotVenvPy
    Write-Host "[supervisor] using .venv: $py" -ForegroundColor DarkGray
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $py = $cmd.Source
        Write-Host "[supervisor] no venv found, using system Python: $py" -ForegroundColor Yellow
    } else {
        Write-Error "Python introuvable. Installe Python ou cree un venv: python -m venv venv"
        exit 1
    }
}

# 3. The supervisor entrypoint must exist.
$entry = Join-Path $root 'scripts\dev_supervisor.py'
if (-not (Test-Path $entry)) {
    Write-Error "Script manquant: $entry"
    exit 1
}

# 4. Warn if the Ops port is already in use (the supervisor would fail to bind).
$opsPort = 8050
try {
    $busy = Get-NetTCPConnection -LocalPort $opsPort -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        Write-Host "[supervisor] WARNING: port $opsPort deja occupe (PID $($busy.OwningProcess | Select-Object -First 1)). " -ForegroundColor Yellow
        Write-Host "             Le supervisor est peut-etre deja lance. Stoppe-le avant de relancer." -ForegroundColor Yellow
    }
} catch { }

# 5. PYTHONPATH so `config`, `workers`, `api` resolve from the repo root.
$env:PYTHONPATH = '.'

# 6. Helpful URLs.
Write-Host ""
Write-Host "=== Antigravity local stack ===" -ForegroundColor Cyan
Write-Host "  Cockpit (frontend) : http://localhost:8000/"
Write-Host "  Ops API            : http://localhost:8050/api/ops/status"
Write-Host "  Ops health         : http://localhost:8050/api/ops/health"
Write-Host "  Ops WebSocket      : ws://localhost:8050/ws/ops"
Write-Host "  Stop               : Ctrl+C  (ou tache 'Stop Full Stack')"
Write-Host ""

# 7. Run (foreground — logs stream into this terminal; Ctrl+C stops cleanly).
& $py .\scripts\dev_supervisor.py
exit $LASTEXITCODE
