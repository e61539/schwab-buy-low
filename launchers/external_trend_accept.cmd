@echo off
setlocal EnableExtensions
REM PATH wrapper for Trend Rider manual acceptance tracking. No live orders are placed.

set "ROOT=C:\Users\cheng_hamn078\scripts\schwab-buy-low"
set "LAUNCHER=%ROOT%\launchers\trend_accept.cmd"

if not exist "%LAUNCHER%" (
  echo [ERR] Missing Trend Rider acceptance launcher: %LAUNCHER%
  exit /b 1
)

call "%LAUNCHER%" %*
exit /b %ERRORLEVEL%
