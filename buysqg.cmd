@echo off
setlocal EnableExtensions

REM Legacy orchestration compatibility
REM Main BuyLow multi-window launcher. This preserves the previous buysqg
REM workflow that opens one PowerShell worker window per BuyLow symbol.
REM launchers\run_buylow.cmd remains the direct single-process engine launcher.

set "ROOT=%~dp0"
pushd "%ROOT%" || exit /b 1

set "BUYLOW_HOME=%CD%"
set "BUYLOW_LOCK_DIR=%CD%\runtime\locks"
set "BUYLOW_TOKENS_FILE=C:\temp\tokens.txt"
set "APPROVED_SYMBOLS=C:\temp\approved_symbols.txt"

REM ---------- Python ----------
set "PY=C:\python313\python.exe"
if not exist "%PY%" set "PY=python"

REM ---------- Script & PowerShell worker ----------
set "BUY_SCRIPT=%CD%\strategies\buylow\buylow_new.py"
set "WRAPPER=%CD%\strategies\buylow\run_one_symbol_buy_quiet.ps1"

if /I "%~1"=="--check" goto check

if not exist "%BUY_SCRIPT%" (
  echo [ERR] Can't find BuyLow script: %BUY_SCRIPT%
  pause
  popd
  exit /b 1
)
if not exist "%WRAPPER%" (
  echo [ERR] Missing PowerShell worker: %WRAPPER%
  pause
  popd
  exit /b 1
)

REM ---------- Session / order flags ----------
set "TZ=America/Detroit"
set "HOURS=regular"
set "ORDER_STYLE=limit"
set "MAX_SLIPPAGE=0.003"
set "EXPOSURE_OPTS=--exp-cap 0.72"

REM ---------- Policy / brakes / guards ----------
set "SOFT=8"
set "HARD=15"
set "BRAKE_OPTS=--soft-brake %SOFT% --hard-brake %HARD% --brake-verbose"
set "POLICY_OPTS=--strict-atr --gate-mode max --dip-baseline prevclose --no-spread-override --min-qty 1 --min-usd 100 --spread-limits DEFAULT=10,GLD=8,QQQ=6,SPY=5,AAPL=5,NVDA=5,MSFT=6 --batch-stages"

REM ---------- Per-symbol sizing ----------
set "USD_SPY=24000"
set "USD_QQQ=10000"
set "USD_GLD=1000"
set "USD_AAPL=1000"
set "USD_NVDA=1000"
set "USD_MSFT=1000"

REM ---------- Logs stay external ----------
set "LOG_IRA1=C:\temp\logs_ira1"
if not exist "%LOG_IRA1%" mkdir "%LOG_IRA1%"
if not exist "%BUYLOW_LOCK_DIR%" mkdir "%BUYLOW_LOCK_DIR%"

REM ---------- PowerShell host ----------
set "PSH=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSH%" (
  echo [ERR] Missing PowerShell host: %PSH%
  pause
  popd
  exit /b 1
)

REM ---------- Looping options ----------
REM ETF windows remain in preview mode. Stock-engine candidates below are live-confirmed.
set "CONFIRM_FLAG="
set "STOCK_CONFIRM_FLAG=--confirm"
REM NOTE: Keep exactly one --confirm overall to avoid duplicates.
set "LOOP_OPTS=--loop --interval-sec 30 --cooldown-sec 600 --acct IRA1 %CONFIRM_FLAG% --buy-dic %CD%\config\buy.dic --atrk-file %CD%\config\atrk.json --log-dir %LOG_IRA1%"
set "STOCK_LOOP_OPTS=--loop --interval-sec 30 --cooldown-sec 600 --acct IRA1 %STOCK_CONFIRM_FLAG% --buy-dic %CD%\config\buy.dic --atrk-file %CD%\config\atrk.json --log-dir %LOG_IRA1%"

REM ===== Launch IRA1 windows =====
start "IRA1 SPY" "%PSH%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "BUY SPY" -Python "%PY%" -Script "%BUY_SCRIPT%" -Symbol SPY -Usd %USD_SPY% ^
  -OrderStyle %ORDER_STYLE% -MaxSlippage %MAX_SLIPPAGE% -Tz "%TZ%" -Hours %HOURS% -LogDir "%LOG_IRA1%" ^
  -ExtraArgs "%BRAKE_OPTS% %POLICY_OPTS% %EXPOSURE_OPTS% %LOOP_OPTS%"

start "IRA1 QQQ" "%PSH%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "BUY QQQ" -Python "%PY%" -Script "%BUY_SCRIPT%" -Symbol QQQ -Usd %USD_QQQ% ^
  -OrderStyle %ORDER_STYLE% -MaxSlippage %MAX_SLIPPAGE% -Tz "%TZ%" -Hours %HOURS% -LogDir "%LOG_IRA1%" ^
  -ExtraArgs "%BRAKE_OPTS% %POLICY_OPTS% %EXPOSURE_OPTS% %LOOP_OPTS%"

start "IRA1 GLD" "%PSH%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "BUY GLD" -Python "%PY%" -Script "%BUY_SCRIPT%" -Symbol GLD -Usd %USD_GLD% ^
  -OrderStyle %ORDER_STYLE% -MaxSlippage %MAX_SLIPPAGE% -Tz "%TZ%" -Hours %HOURS% -LogDir "%LOG_IRA1%" ^
  -ExtraArgs "%BRAKE_OPTS% %POLICY_OPTS% %EXPOSURE_OPTS% %LOOP_OPTS%"

REM ===== Satellite stock-engine candidates require approved_symbols.txt =====
if not exist "%APPROVED_SYMBOLS%" (
  echo [INFO] No approved stock symbols file found: %APPROVED_SYMBOLS%
  goto done
)

findstr /I /X "AAPL" "%APPROVED_SYMBOLS%" >nul
if not errorlevel 1 start "IRA1 AAPL" "%PSH%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "BUY AAPL" -Python "%PY%" -Script "%BUY_SCRIPT%" -Symbol AAPL -Usd %USD_AAPL% ^
  -OrderStyle %ORDER_STYLE% -MaxSlippage %MAX_SLIPPAGE% -Tz "%TZ%" -Hours %HOURS% -LogDir "%LOG_IRA1%" ^
  -ExtraArgs "%BRAKE_OPTS% %POLICY_OPTS% %EXPOSURE_OPTS% %STOCK_LOOP_OPTS%"

findstr /I /X "NVDA" "%APPROVED_SYMBOLS%" >nul
if not errorlevel 1 start "IRA1 NVDA" "%PSH%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "BUY NVDA" -Python "%PY%" -Script "%BUY_SCRIPT%" -Symbol NVDA -Usd %USD_NVDA% ^
  -OrderStyle %ORDER_STYLE% -MaxSlippage %MAX_SLIPPAGE% -Tz "%TZ%" -Hours %HOURS% -LogDir "%LOG_IRA1%" ^
  -ExtraArgs "%BRAKE_OPTS% %POLICY_OPTS% %EXPOSURE_OPTS% %STOCK_LOOP_OPTS%"

findstr /I /X "MSFT" "%APPROVED_SYMBOLS%" >nul
if not errorlevel 1 start "IRA1 MSFT" "%PSH%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "BUY MSFT" -Python "%PY%" -Script "%BUY_SCRIPT%" -Symbol MSFT -Usd %USD_MSFT% ^
  -OrderStyle %ORDER_STYLE% -MaxSlippage %MAX_SLIPPAGE% -Tz "%TZ%" -Hours %HOURS% -LogDir "%LOG_IRA1%" ^
  -ExtraArgs "%BRAKE_OPTS% %POLICY_OPTS% %EXPOSURE_OPTS% %STOCK_LOOP_OPTS%"

:done
popd
exit /b 0

:check
echo ROOT=%CD%
echo PY=%PY%
echo BUY_SCRIPT=%BUY_SCRIPT%
echo WRAPPER=%WRAPPER%
echo LOG_IRA1=C:\temp\logs_ira1
echo BUYLOW_LOCK_DIR=%BUYLOW_LOCK_DIR%
if not exist "%BUY_SCRIPT%" (
  echo [ERR] Can't find BuyLow script: %BUY_SCRIPT%
  popd
  exit /b 1
)
if not exist "%WRAPPER%" (
  echo [ERR] Missing PowerShell worker: %WRAPPER%
  popd
  exit /b 1
)
echo [OK] buysqg orchestration path check passed
popd
exit /b 0
