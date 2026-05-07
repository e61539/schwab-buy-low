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

set "SCRIPT=%ROOT%\strategies\sellhigh\sell_high_pct_patched_staged.py"
set "SELL_DIC=%ROOT%\strategies\sellhigh\sell.dic"
set "LOG_DIR=C:\temp\logs_sell"
set "LOCK_DIR=%ROOT%\runtime\locks"
set "BUYLOW_HOME=%ROOT%"

if /I "%~1"=="--check" goto check

if "%~1"=="" (
  echo [ERR] Pass SellHigh arguments, for example:
  echo       run_sellhigh.cmd SPY --sell-dic "%SELL_DIC%" --hours extended
  popd
  exit /b 2
)
if not exist "%SCRIPT%" (
  echo [ERR] Missing SellHigh script: %SCRIPT%
  popd
  exit /b 1
)
if not exist "%SELL_DIC%" (
  echo [ERR] Missing sell.dic: %SELL_DIC%
  popd
  exit /b 1
)
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not exist "%LOCK_DIR%" mkdir "%LOCK_DIR%"

"%PY%" -u -W ignore::SyntaxWarning "%SCRIPT%" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
popd
exit /b %CODE%

:check
echo ROOT=%ROOT%
echo PY=%PY%
echo SCRIPT=%SCRIPT%
echo SELL_DIC=%SELL_DIC%
echo LOG_DIR=%LOG_DIR%
echo LOCK_DIR=%LOCK_DIR%
if not exist "%SCRIPT%" (
  echo [ERR] Missing SellHigh script: %SCRIPT%
  popd
  exit /b 1
)
if not exist "%SELL_DIC%" (
  echo [ERR] Missing sell.dic: %SELL_DIC%
  popd
  exit /b 1
)
echo [OK] run_sellhigh.cmd path check passed
popd
exit /b 0
