@echo off
setlocal EnableExtensions
REM Phase 2 path hardening: resolve paths from this launcher location.

set "LAUNCHER_DIR=%~dp0"
pushd "%LAUNCHER_DIR%.." || exit /b 1
set "ROOT=%CD%"

if exist "C:\python313\python.exe" (
  set "PY=C:\python313\python.exe"
) else (
  set "PY=python"
)

set "SCRIPT=%ROOT%\strategies\buylow\buylow_new.py"
set "LOG_DIR=C:\temp\logs_ira1"
set "LOCK_DIR=%ROOT%\runtime\locks"
set "BUYLOW_HOME=%ROOT%"

if /I "%~1"=="--check" goto check

if not exist "%SCRIPT%" (
  echo [ERR] Missing BuyLow script: %SCRIPT%
  popd
  exit /b 1
)
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not exist "%LOCK_DIR%" mkdir "%LOCK_DIR%"

"%PY%" -u -W ignore::SyntaxWarning "%SCRIPT%" --log-dir "%LOG_DIR%" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
popd
exit /b %CODE%

:check
echo ROOT=%ROOT%
echo PY=%PY%
echo SCRIPT=%SCRIPT%
echo LOG_DIR=%LOG_DIR%
echo LOCK_DIR=%LOCK_DIR%
if not exist "%SCRIPT%" (
  echo [ERR] Missing BuyLow script: %SCRIPT%
  popd
  exit /b 1
)
echo [OK] run_buylow.cmd path check passed
popd
exit /b 0
