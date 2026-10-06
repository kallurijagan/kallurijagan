<#
.SYNOPSIS
  Run the automated tests (backend pytest suite + dashboard type-check/build).
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { Write-Host "Run .\setup.ps1 first." -ForegroundColor Red; exit 1 }

Push-Location (Join-Path $Root "backend")
try { & $VenvPy -m pytest; $backend = $LASTEXITCODE } finally { Pop-Location }

Push-Location (Join-Path $Root "frontend")
try { & npm run build; $frontend = $LASTEXITCODE } finally { Pop-Location }

if ($backend -ne 0 -or $frontend -ne 0) {
    Write-Host "FAILED (backend=$backend frontend=$frontend)" -ForegroundColor Red
    exit 1
}
Write-Host "All tests passed and the dashboard builds." -ForegroundColor Green
