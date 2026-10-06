<#
.SYNOPSIS
  Gracefully stop the running paper bot (finishes the current engine tick, records a final
  equity snapshot, releases the engine lock). Open orders/positions are recovered at the
  next start; pending settlements are checked again then.

.EXAMPLE
  .\stop.ps1
  .\stop.ps1 -Port 8001
#>
[CmdletBinding()]
param([int]$Port = 0)

$ErrorActionPreference = "Stop"
# Same port resolution as start.ps1: -Port, then KBOT_PORT in .env, then 8000.
if ($Port -le 0) {
    $Port = 8000
    $EnvFile = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) ".env"
    if (Test-Path $EnvFile) {
        foreach ($line in (Get-Content $EnvFile)) {
            if ($line -match '^\s*KBOT_PORT\s*=\s*["'']?(\d+)["'']?\s*$') { $Port = [int]$Matches[1]; break }
        }
    }
}
try {
    $r = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/admin/shutdown" -TimeoutSec 10
    Write-Host $r.message -ForegroundColor Green
} catch {
    Write-Host "Could not reach the bot on port ${Port}: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "If it is running in a window, press Ctrl+C there."
    exit 1
}
