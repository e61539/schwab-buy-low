@echo off
setlocal EnableExtensions
REM Phase 2 path hardening: resolve paths from this launcher location.
REM Trend Rider is proposal-only. This launcher does not place orders.

set "LAUNCHER_DIR=%~dp0"
pushd "%LAUNCHER_DIR%.." || exit /b 1
set "ROOT=%CD%"

if exist "C:\python313\python.exe" (
  set "PY=C:\python313\python.exe"
) else (
  set "PY=python"
)

set "SCRIPT=%ROOT%\strategies\trend_rider\trend_rider.py"
set "BUYLOW_HOME=%ROOT%"

if /I "%~1"=="--check" goto check

if not exist "%SCRIPT%" (
  echo [ERR] Missing Trend Rider placeholder: %SCRIPT%
  popd
  exit /b 1
)

"%PY%" -u "%SCRIPT%" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
popd
exit /b %CODE%

:check
echo ROOT=%ROOT%
echo PY=%PY%
echo SCRIPT=%SCRIPT%
if not exist "%SCRIPT%" (
  echo [ERR] Missing Trend Rider placeholder: %SCRIPT%
  popd
  exit /b 1
)
echo [OK] run_trend.cmd path check passed
popd
exit /b 0
