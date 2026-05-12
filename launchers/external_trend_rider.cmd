@echo off
setlocal EnableExtensions
REM PATH wrapper for Trend Rider proposal-only shortlist. No live orders are placed.

set "ROOT=C:\Users\cheng_hamn078\scripts\schwab-buy-low"
set "LAUNCHER=%ROOT%\launchers\run_trend.cmd"

if not exist "%LAUNCHER%" (
  echo [ERR] Missing Trend Rider launcher: %LAUNCHER%
  exit /b 1
)

call "%LAUNCHER%" %*
exit /b %ERRORLEVEL%
