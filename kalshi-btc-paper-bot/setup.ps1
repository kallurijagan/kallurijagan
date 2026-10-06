<#
.SYNOPSIS
  One-time setup for the Kalshi BTC 15-minute PAPER trading bot (Windows).

.DESCRIPTION
  - Checks Python 3.12+ and Node.js 20.19+ / npm.
  - Creates backend\.venv and installs the pinned Python lockfiles.
  - Installs the dashboard with `npm ci` (package-lock.json) and builds it.
  - Creates .env from .env.example (no secrets needed) and the data folder.
  Safe to re-run.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup.ps1
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -RunTests
#>
[CmdletBinding()]
param(
    [switch]$RunTests,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Venv = Join-Path $Backend ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

function Find-Python {
    $candidates = @(
        @("py", "-3.13"), @("py", "-3.12"), @("py", "-3"), @("python", $null), @("python3", $null)
    )
    foreach ($c in $candidates) {
        $exe = $c[0]; $arg = $c[1]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            if ($arg) { $v = & $exe $arg -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null }
            else { $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null }
        } catch { continue }
        if (-not $v) { continue }
        $parts = "$v".Trim().Split(".")
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
            return @{ Exe = $exe; Arg = $arg; Version = "$v".Trim() }
        }
    }
    return $null
}

Write-Host ""
Write-Host "Kalshi BTC 15m PAPER bot - setup (simulated money only; no real orders are ever sent)" -ForegroundColor Yellow
Write-Host ""

Write-Step "Checking Python 3.12+"
$py = Find-Python
if (-not $py) { Fail "Python 3.12 or newer not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and re-run." }
Write-Host "    found Python $($py.Version) ($($py.Exe) $($py.Arg))"

if (-not $SkipFrontend) {
    Write-Step "Checking Node.js 20.19+ and npm"
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail "Node.js not found. Install the LTS version from https://nodejs.org/ and re-run." }
    $nodeVersion = (& node --version).TrimStart("v")
    $np = $nodeVersion.Split(".")
    $nodeOk = ([int]$np[0] -gt 20) -or (([int]$np[0] -eq 20) -and ([int]$np[1] -ge 19))
    if (-not $nodeOk) { Fail "Node.js $nodeVersion is too old; version 20.19+ (or 22.12+) is required." }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Fail "npm not found (it ships with Node.js)." }
    Write-Host "    found Node.js $nodeVersion"
}

Write-Step "Creating Python virtual environment in backend\.venv"
if (-not (Test-Path $VenvPy)) {
    if ($py.Arg) { & $py.Exe $py.Arg -m venv $Venv } else { & $py.Exe -m venv $Venv }
    if ($LASTEXITCODE -ne 0) { Fail "could not create the virtual environment" }
}
& $VenvPy -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed" }

Write-Step "Installing pinned Python dependencies (requirements.txt + requirements-dev.txt)"
& $VenvPy -m pip install --quiet -r (Join-Path $Backend "requirements.txt") -r (Join-Path $Backend "requirements-dev.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }

if (-not $SkipFrontend) {
    Write-Step "Installing dashboard dependencies (npm ci, from package-lock.json)"
    Push-Location $Frontend
    try {
        & npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { Fail "npm ci failed" }
        Write-Step "Building the dashboard (frontend\dist)"
        & npm run build
        if ($LASTEXITCODE -ne 0) { Fail "dashboard build failed" }
    } finally { Pop-Location }
}

Write-Step "Preparing configuration and data folder"
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
    Write-Host "    created .env from .env.example (no secrets required)"
} else {
    Write-Host "    .env already exists - left unchanged"
}
New-Item -ItemType Directory -Force -Path (Join-Path $Root "data") | Out-Null

if ($RunTests) {
    Write-Step "Running backend tests"
    Push-Location $Backend
    try {
        & $VenvPy -m pytest
        if ($LASTEXITCODE -ne 0) { Fail "tests failed" }
    } finally { Pop-Location }
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Start the bot:       .\start.ps1"
Write-Host "Dashboard URL:       http://127.0.0.1:8000"
Write-Host "Preview (sample):    .\start.ps1 -Preview"
Write-Host "Live data check:     .\start.ps1 -LiveCheck"
Write-Host ""
