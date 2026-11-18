@echo off
rem S3 trade-tape backtest of the last 24 hours on Kalshi PUBLIC data (SIMULATED, paper only).
rem Read-only: GET requests to public market-data endpoints, no API key, cannot place orders.
rem Place this folder at edge_kalshi\backtests\s3tape\ and run: backtests\s3tape\run_24h.cmd
setlocal
set "HERE=%~dp0"
for %%I in ("%HERE%..\..") do set "ROOT=%%~fI"
set "PY=%ROOT%\.venv-bt\Scripts\python.exe"
if not exist "%PY%" (
  echo Python not found at "%PY%".
  exit /b 1
)
pushd "%HERE%"
"%PY%" -m s3tape.fetch --hours 24 --out "%ROOT%\kalshi_live\public_tape"
if errorlevel 1 (
  popd
  exit /b 1
)
"%PY%" -m s3tape.run --latest "%ROOT%\kalshi_live\public_tape" --out "%ROOT%\backtests\results\s3_tape_recent"
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
