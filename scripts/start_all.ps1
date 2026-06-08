# ============================================================
# Start Full Stack (Windows PowerShell)
# The dev supervisor already owns docker compose + bootstrap +
# workers + API, so "full stack" == start the supervisor in its
# own window and open the cockpit. We do NOT relaunch any process
# the supervisor is responsible for (no double launch).
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_all.ps1
# ============================================================

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Guard against a double launch: if the Ops port is already listening,
# the supervisor is already up — just open the cockpit instead of starting again.
$alreadyUp = $false
try {
    if (Get-NetTCPConnection -LocalPort 8050 -State Listen -ErrorAction SilentlyContinue) { $alreadyUp = $true }
} catch { }

if ($alreadyUp) {
    Write-Host "[start_all] Supervisor deja en cours (port 8050 occupe). Ouverture du cockpit." -ForegroundColor Yellow
} else {
    $supScript = Join-Path $root 'scripts\start_dev_supervisor.ps1'
    Write-Host "[start_all] Lancement du supervisor dans une nouvelle fenetre PowerShell..." -ForegroundColor Cyan
    # New window so the supervisor logs are isolated and this script can return.
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $supScript) `
        -WorkingDirectory $root
    Start-Sleep -Seconds 3
}

# Open the cockpit in the default browser (best-effort).
try {
    Start-Process 'http://localhost:8000/'
} catch {
    Write-Host "[start_all] Ouvre manuellement http://localhost:8000/" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Cockpit : http://localhost:8000/" -ForegroundColor Green
Write-Host "Ops API : http://localhost:8050/api/ops/health" -ForegroundColor Green
Write-Host "Stop    : tache 'Stop Full Stack' ou .\scripts\stop_all.ps1" -ForegroundColor Green
