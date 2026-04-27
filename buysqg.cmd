@echo off
setlocal

REM ===== BUY launcher (staged + loop) =====
REM Requires:
REM   - buylow_new.py
REM   - run_one_symbol_buy_quiet.ps1
REM   - env vars app_key/app_secret set for schwabdev
REM Optional:
REM   - config\sym_caps.dic (per-symbol exposure caps)
REM   - config\sym_overrides.json (per-symbol max_slippage/min_usd for partial sizing)

set "ROOT=%~dp0"

REM ---------- Python ----------
set "PY=C:\python313\python.exe"
if not exist "%PY%" set "PY=python"

REM ---------- Script & Wrapper ----------
set "BUY_SCRIPT=%ROOT%buylow_new.py"
if not exist "%BUY_SCRIPT%" (
  echo [ERR] Can't find: %BUY_SCRIPT%
  pause & exit /b 1
)
set "WRAPPER=%ROOT%run_one_symbol_buy_quiet.ps1"
if not exist "%WRAPPER%" (
  echo [ERR] Missing wrapper: %WRAPPER%
  pause & exit /b 1
)

REM ---------- Session / order flags ----------
set "TZ=America/Detroit"
set "HOURS=regular"
set "ORDER_STYLE=limit"
set "MAX_SLIPPAGE=0.003"
set "EXPOSURE_OPTS=--exp-cap 0.70"

REM ---------- Policy / brakes / guards ----------
set "SOFT=8"
set "HARD=15"
set "BRAKE_OPTS=--soft-brake %SOFT% --hard-brake %HARD% --brake-verbose"

set "POLICY_OPTS=--strict-atr --gate-mode max --dip-baseline prevclose --no-spread-override --min-qty 1 --min-usd 100 --spread-limits DEFAULT=10,GLD=8,QQQ=6,SPY=5 --batch-stages"

REM ---------- Per-symbol sizing (stage-1 conservative budgets) ----------
REM ---- USD defaults for ETFSelector-approved ETFs ----
set "USD_SPY=24000"
set "USD_QQQ=10000"
set "USD_GLD=1000"

REM ---------- Logs ----------
set "LOG_IRA1=%ROOT%runtime\logs"
if not exist "%LOG_IRA1%" mkdir "%LOG_IRA1%"

REM ---------- PowerShell host ----------
set "PSH=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

REM --------- Keep your existing generic POLICY_OPTS/LOOP_OPTS for others ---------

REM ---------- Looping options ----------
REM This repo launcher defaults to preview mode. Set CONFIRM_FLAG=--confirm for live orders.
set "CONFIRM_FLAG="
REM set "CONFIRM_FLAG=--confirm"
REM NOTE: Keep exactly one --confirm overall to avoid duplicates.
set "LOOP_OPTS=--loop --interval-sec 30 --cooldown-sec 600 --acct IRA1 %CONFIRM_FLAG% --buy-dic %ROOT%config\buy.dic --atrk-file %ROOT%config\atrk.json --log-dir %LOG_IRA1%"

REM ===== Launch IRA1 windows (remove -NoExit if you want windows to close) =====
start "IRA1 SPY"  "%PSH%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "BUY SPY" -Python "%PY%" -Script "%BUY_SCRIPT%" -Symbol SPY -Usd %USD_SPY%  ^
  -OrderStyle %ORDER_STYLE% -MaxSlippage %MAX_SLIPPAGE% -Tz "%TZ%" -Hours %HOURS% -LogDir "%LOG_IRA1%" ^
  -ExtraArgs "%BRAKE_OPTS% %POLICY_OPTS% %EXPOSURE_OPTS% %LOOP_OPTS%"

start "IRA1 QQQ"  "%PSH%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "BUY QQQ" -Python "%PY%" -Script "%BUY_SCRIPT%" -Symbol QQQ -Usd %USD_QQQ% ^
  -OrderStyle %ORDER_STYLE% -MaxSlippage %MAX_SLIPPAGE% -Tz "%TZ%" -Hours %HOURS% -LogDir "%LOG_IRA1%" ^
  -ExtraArgs "%BRAKE_OPTS% %POLICY_OPTS% %EXPOSURE_OPTS% %LOOP_OPTS%"

start "IRA1 GLD" "%PSH%" -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%WRAPPER%" ^
  -Title "BUY GLD" -Python "%PY%" -Script "%BUY_SCRIPT%" -Symbol GLD -Usd %USD_GLD%  ^
  -OrderStyle %ORDER_STYLE% -MaxSlippage %MAX_SLIPPAGE% -Tz "%TZ%" -Hours %HOURS% -LogDir "%LOG_IRA1%" ^
  -ExtraArgs "%BRAKE_OPTS% %POLICY_OPTS% %EXPOSURE_OPTS% %LOOP_OPTS%"

endlocal
exit /b 0
