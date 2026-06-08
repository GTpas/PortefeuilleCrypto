# ============================================================
# Stop Full Stack (Windows PowerShell)
# Stops the supervisor + any child processes it launched, then
# brings down the docker infra. Safe to run repeatedly.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_all.ps1
# ============================================================

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[stop_all] Arret des process Python du stack (supervisor, uvicorn, workers)..." -ForegroundColor Cyan

# Match the supervisor + the children it spawns (uvicorn api, workers.*).
# Win32_Process.CommandLine lets us target only THIS project's processes.
$patterns = 'dev_supervisor\.py|uvicorn\s+api\.main|workers\.(ingestor|aggregator|feature_worker|social_ingestor|antigravity_bot|bootstrap)'
try {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='python3.exe'" |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match $patterns) }
    foreach ($p in $procs) {
        Write-Host ("  kill PID {0}: {1}" -f $p.ProcessId, ($p.CommandLine.Substring(0, [Math]::Min(80, $p.CommandLine.Length)))) -ForegroundColor DarkGray
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if (-not $procs) { Write-Host "  (aucun process Python du stack trouve)" -ForegroundColor DarkGray }
} catch {
    Write-Host "  WARNING: impossible d'enumerer les process ($_)" -ForegroundColor Yellow
}

# Bring down docker infra (db + redis).
Write-Host "[stop_all] docker compose down..." -ForegroundColor Cyan
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker compose down
} else {
    Write-Host "  docker introuvable, infra non arretee." -ForegroundColor Yellow
}

Write-Host "[stop_all] Termine." -ForegroundColor Green
