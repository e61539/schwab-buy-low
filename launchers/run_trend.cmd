@echo off
setlocal EnableExtensions
REM Phase 2 path hardening: resolve paths from this launcher location.
REM Trend Rider is proposal-only. This launcher does not place orders.
REM Phase 3: refresh daily CSV history before running proposals.

set "LAUNCHER_DIR=%~dp0"
pushd "%LAUNCHER_DIR%.." || exit /b 1
set "ROOT=%CD%"

if exist "C:\python313\python.exe" (
  set "PY=C:\python313\python.exe"
) else (
  set "PY=python"
)

set "SCRIPT=%ROOT%\strategies\trend_rider\trend_rider.py"
set "REFRESH_SCRIPT=%ROOT%\strategies\trend_rider\refresh_history.py"
set "BUYLOW_HOME=%ROOT%"

if /I "%~1"=="--check" goto check

if not exist "%SCRIPT%" (
  echo [ERR] Missing Trend Rider placeholder: %SCRIPT%
  popd
  exit /b 1
)

if not exist "%REFRESH_SCRIPT%" (
  echo [ERR] Missing Trend Rider history refresh script: %REFRESH_SCRIPT%
  popd
  exit /b 1
)
echo [INFO] Refreshing Trend Rider CSV history...
"%PY%" -u "%REFRESH_SCRIPT%"
set "REFRESH_CODE=%ERRORLEVEL%"
if not "%REFRESH_CODE%"=="0" (
  echo [ERR] Trend Rider history refresh failed with exit code %REFRESH_CODE%
  pause
  popd
  exit /b %REFRESH_CODE%
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
echo REFRESH_SCRIPT=%REFRESH_SCRIPT%
if not exist "%SCRIPT%" (
  echo [ERR] Missing Trend Rider placeholder: %SCRIPT%
  popd
  exit /b 1
)
if not exist "%REFRESH_SCRIPT%" (
  echo [ERR] Missing Trend Rider history refresh script: %REFRESH_SCRIPT%
  popd
  exit /b 1
)
echo [OK] run_trend.cmd path check passed
popd
exit /b 0
