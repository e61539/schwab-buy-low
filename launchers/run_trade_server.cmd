@echo off
setlocal EnableExtensions
REM Phase 2 path hardening: resolve paths from this launcher location.

set "LAUNCHER_DIR=%~dp0"
pushd "%LAUNCHER_DIR%.." || exit /b 1
set "ROOT=%CD%"
set "BUYLOW_HOME=%ROOT%"
set "BUYLOW_LOCK_DIR=%ROOT%\runtime\locks"

if exist "C:\python313\python.exe" (
  set "PY=C:\python313\python.exe"
) else (
  set "PY=python"
)

set "APP_MODULE="
if exist "%ROOT%\trade_server.py" set "APP_MODULE=trade_server:app"
if not defined APP_MODULE if exist "%ROOT%\dashboard\trade_server.py" set "APP_MODULE=dashboard.trade_server:app"

if /I "%~1"=="--check" goto check

if not defined APP_MODULE (
  echo [ERR] Could not find trade_server.py under %ROOT% or %ROOT%\dashboard
  pause
  popd
  exit /b 1
)
if not exist "%BUYLOW_LOCK_DIR%" mkdir "%BUYLOW_LOCK_DIR%"

"%PY%" -m uvicorn %APP_MODULE% --host 0.0.0.0 --port 8080
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
popd
exit /b %CODE%

:check
echo ROOT=%ROOT%
echo PY=%PY%
echo APP_MODULE=%APP_MODULE%
echo BUYLOW_LOCK_DIR=%BUYLOW_LOCK_DIR%
if not defined APP_MODULE (
  echo [ERR] Could not find trade_server.py under %ROOT% or %ROOT%\dashboard
  popd
  exit /b 1
)
echo [OK] run_trade_server.cmd path check passed
popd
exit /b 0
