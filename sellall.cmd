@echo off
setlocal EnableExtensions

REM Legacy orchestration compatibility
REM Main SellHigh multi-window launcher. This preserves the previous sellall
REM workflow that opens one PowerShell worker window per SellHigh symbol.
REM launchers\run_sellhigh.cmd remains the direct single-process engine launcher.

set "ROOT=%~dp0"
pushd "%ROOT%" || exit /b 1

set "SELLHIGH_DIR=%CD%\strategies\sellhigh"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "WRAPPER=%SELLHIGH_DIR%\run_one_symbol_sell_quiet.ps1"
set "PY=C:\python313\python.exe"
if not exist "%PY%" set "PY=python"
set "SCRIPT=%SELLHIGH_DIR%\sell_high_pct_patched_staged.py"
set "SELL_DIC=%SELLHIGH_DIR%\sell.dic"
set "LOG_DIR=C:\temp\logs_sell"
set "BUYLOW_HOME=%CD%"
set "SELL_DIC=%SELL_DIC%"

if /I "%~1"=="--check" goto check

REM ===== Session/options =====
set "TZ=America/Detroit"
set "HOURS=extended"
set "ON_CLOSE=sleep"

REM One place to tweak cadence & sizing. Keep previous live behavior.
set "EXTRA=--acct IRA1 --interval 30 --cooldown 600 --min-shares 1 --sell-frac 1.0 --confirm --show-when-zero --show-why --verbose"
REM DRY mode example:
REM set "EXTRA=--acct IRA1 --interval 30 --cooldown 600 --min-shares 1 --sell-frac 1.0 --show-when-zero --show-why"

REM ===== Sanity checks =====
if not exist "%PS%" (
  echo [ERR] Missing PowerShell: %PS%
  pause
  popd
  exit /b 1
)
if not exist "%WRAPPER%" (
  echo [ERR] Missing worker: %WRAPPER%
  pause
  popd
  exit /b 1
)
if not exist "%SCRIPT%" (
  echo [ERR] Missing SellHigh script: %SCRIPT%
  pause
  popd
  exit /b 1
)
if not exist "%SELL_DIC%" (
  echo [ERR] Missing sell.dic: %SELL_DIC%
  pause
  popd
  exit /b 1
)
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ===== Launch one PowerShell window per symbol =====
call :launch SPY
call :launch QQQ
call :launch GLD
REM call :launch NVDA
REM call :launch FIG
REM call :launch TSM
REM call :launch IBIT
REM call :launch EETH
REM call :launch COST

popd
exit /b 0

:launch
start "SELL %~1" "%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "SELL %~1" ^
  -Symbol "%~1" ^
  -Script "%SCRIPT%" ^
  -SellDic "%SELL_DIC%" ^
  -Tz "%TZ%" ^
  -Hours "%HOURS%" ^
  -OnClose "%ON_CLOSE%" ^
  -LogDir "%LOG_DIR%" ^
  -Python "%PY%" ^
  -NoPause ^
  -ExtraArgs "%EXTRA%"
exit /b

:check
echo ROOT=%CD%
echo SELLHIGH_DIR=%SELLHIGH_DIR%
echo PY=%PY%
echo WRAPPER=%WRAPPER%
echo SCRIPT=%SCRIPT%
echo SELL_DIC=%SELL_DIC%
echo LOG_DIR=%LOG_DIR%
if not exist "%WRAPPER%" (
  echo [ERR] Missing worker: %WRAPPER%
  popd
  exit /b 1
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
echo [OK] sellall orchestration path check passed
popd
exit /b 0
