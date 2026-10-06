<#
.SYNOPSIS
  Start the Kalshi BTC 15-minute PAPER trading bot (backend engine + dashboard).

.DESCRIPTION
  Starts ONE backend process bound to 127.0.0.1. It runs the paper engine and serves the
  built dashboard at http://127.0.0.1:8000 (or -Port). The engine keeps running when the
  browser tab is closed; keep this window (and the computer) running for live paper
  trading. Press Ctrl+C here, or run .\stop.ps1, for a graceful shutdown.

.PARAMETER Preview
  Use clearly labeled SYNTHETIC sample data and a separate preview database.
.PARAMETER SeedPreview
  With -Preview: first fast-forward 6 hours of sample history so the analytics have data.
.PARAMETER Dev
  Also start the Vite dev server with hot reload at http://127.0.0.1:5173.
.PARAMETER LiveCheck
  Only run the read-only live Kalshi data check and exit (no trading, no database).
.PARAMETER NoBrowser
  Do not open the dashboard in the default browser.

.EXAMPLE
  .\start.ps1
  .\start.ps1 -Preview -SeedPreview
  .\start.ps1 -LiveCheck
#>
[CmdletBinding()]
param(
    [switch]$Preview,
    [switch]$SeedPreview,
    [switch]$Dev,
    [switch]$LiveCheck,
    [switch]$NoBrowser,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$VenvPy = Join-Path $Backend ".venv\Scripts\python.exe"
$Url = "http://127.0.0.1:$Port"

if (-not (Test-Path $VenvPy)) {
    Write-Host "Python environment not found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

if ($LiveCheck) {
    Write-Host "Running read-only live Kalshi data check (GET requests only)..." -ForegroundColor Cyan
    Push-Location $Backend
    try { & $VenvPy -m app.tools.live_check; $code = $LASTEXITCODE } finally { Pop-Location }
    exit $code
}

# Guard: refuse to start a second server on the same port. (The engine itself also holds an
# exclusive lock on data\paper.engine.lock, so two engines can never trade the same account.)
$inUse = $null
try { $inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue } catch { $inUse = $null }
if ($inUse) {
    Write-Host "Port $Port is already in use - the bot may already be running at $Url" -ForegroundColor Yellow
    Write-Host "Open that URL, or stop the other instance with .\stop.ps1 -Port $Port" -ForegroundColor Yellow
    exit 1
}

# Rebuild the dashboard if it is missing or older than its sources.
$Dist = Join-Path $Frontend "dist\index.html"
$needBuild = -not (Test-Path $Dist)
if (-not $needBuild) {
    $distTime = (Get-Item $Dist).LastWriteTimeUtc
    $newer = Get-ChildItem -Path (Join-Path $Frontend "src") -Recurse -File | Where-Object { $_.LastWriteTimeUtc -gt $distTime } | Select-Object -First 1
    if ($newer) { $needBuild = $true }
}
if ($needBuild) {
    if (-not (Test-Path (Join-Path $Frontend "node_modules"))) {
        Write-Host "Dashboard dependencies missing. Run .\setup.ps1 first." -ForegroundColor Red
        exit 1
    }
    Write-Host "Building dashboard..." -ForegroundColor Cyan
    Push-Location $Frontend
    try { & npm run build; if ($LASTEXITCODE -ne 0) { throw "dashboard build failed" } } finally { Pop-Location }
}

$env:KBOT_PORT = "$Port"
if ($Preview) { $env:KBOT_MODE = "preview" } else { $env:KBOT_MODE = "live" }

if ($SeedPreview) {
    if (-not $Preview) { Write-Host "-SeedPreview requires -Preview" -ForegroundColor Red; exit 1 }
    Write-Host "Seeding PREVIEW database with 6 hours of synthetic sample history..." -ForegroundColor Magenta
    Push-Location $Backend
    try { & $VenvPy -m app.tools.seed_preview --hours 6 } finally { Pop-Location }
}

$devProc = $null
if ($Dev) {
    Write-Host "Starting Vite dev server at http://127.0.0.1:5173 (proxying /api to $Url)" -ForegroundColor Cyan
    $devProc = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" -WorkingDirectory $Frontend -PassThru
}

Write-Host ""
if ($Preview) {
    Write-Host "PREVIEW MODE - synthetic SAMPLE data, separate database (data\preview.sqlite3)" -ForegroundColor Magenta
} else {
    Write-Host "PAPER TRADING - real Kalshi public market data, simulated money (data\paper.sqlite3)" -ForegroundColor Yellow
}
Write-Host "Dashboard: $Url" -ForegroundColor Green
Write-Host "Logs:      $(Join-Path $Root 'data\logs')"
Write-Host "Keep this window open. Press Ctrl+C (or run .\stop.ps1) to stop gracefully."
Write-Host ""

if (-not $NoBrowser) {
    $target = $Url
    if ($Dev) { $target = "http://127.0.0.1:5173" }
    Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 4; Start-Process $u } -ArgumentList $target | Out-Null
}

Push-Location $Backend
try {
    & $VenvPy -m app
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    if ($devProc -and -not $devProc.HasExited) { Stop-Process -Id $devProc.Id -ErrorAction SilentlyContinue }
}
exit $code
