@echo off
setlocal EnableExtensions
REM Phase 2 path hardening: resolve paths from this launcher location.

set "LAUNCHER_DIR=%~dp0"
pushd "%LAUNCHER_DIR%.." || exit /b 1
set "ROOT=%CD%"
set "BUYLOW_HOME=%ROOT%"

if exist "C:\python313\python.exe" (
  set "PY=C:\python313\python.exe"
) else (
  set "PY=python"
)

set "APP_MODULE="
if exist "%ROOT%\dashboard\dashboard_api.py" set "APP_MODULE=dashboard.dashboard_api:app"
if not defined APP_MODULE if exist "%ROOT%\dashboard_api.py" set "APP_MODULE=dashboard_api:app"

if /I "%~1"=="--check" goto check

if not defined APP_MODULE (
  echo [ERR] Could not find dashboard_api.py under %ROOT%\dashboard or %ROOT%
  pause
  popd
  exit /b 1
)

"%PY%" -m uvicorn %APP_MODULE% --host 0.0.0.0 --port 8000
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
popd
exit /b %CODE%

:check
echo ROOT=%ROOT%
echo PY=%PY%
echo APP_MODULE=%APP_MODULE%
if not defined APP_MODULE (
  echo [ERR] Could not find dashboard_api.py under %ROOT%\dashboard or %ROOT%
  popd
  exit /b 1
)
echo [OK] run_dashboard.cmd path check passed
popd
exit /b 0
