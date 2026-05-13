@echo off
setlocal EnableExtensions
REM Trade server dashboard runtime is share-friendly and lives outside the protected user profile.

set "LAUNCHER_DIR=%~dp0"
pushd "%LAUNCHER_DIR%.." || exit /b 1
set "ROOT=%CD%"
set "DASHBOARD_DIR=C:\shared_dashboard"
set "BUYLOW_HOME=%ROOT%"
set "BUYLOW_LOCK_DIR=%ROOT%\runtime\locks"

if exist "C:\python313\python.exe" (
  set "PY=C:\python313\python.exe"
) else (
  set "PY=python"
)

set "APP_MODULE=trade_server:app"

if /I "%~1"=="--check" goto check

if not exist "%DASHBOARD_DIR%\trade_server.py" (
  echo [ERR] Could not find trade_server.py under %DASHBOARD_DIR%
  pause
  popd
  exit /b 1
)
if not exist "%BUYLOW_LOCK_DIR%" mkdir "%BUYLOW_LOCK_DIR%"

pushd "%DASHBOARD_DIR%" || exit /b 1
"%PY%" -m uvicorn %APP_MODULE% --host 0.0.0.0 --port 8080
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
popd
popd
exit /b %CODE%

:check
echo ROOT=%ROOT%
echo DASHBOARD_DIR=%DASHBOARD_DIR%
echo PY=%PY%
echo APP_MODULE=%APP_MODULE%
echo BUYLOW_LOCK_DIR=%BUYLOW_LOCK_DIR%
if not exist "%DASHBOARD_DIR%\trade_server.py" (
  echo [ERR] Could not find trade_server.py under %DASHBOARD_DIR%
  popd
  exit /b 1
)
echo [OK] run_trade_server.cmd path check passed
popd
exit /b 0
