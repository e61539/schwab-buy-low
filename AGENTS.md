# BuyLow Backend Agent Guide

## 1. System Overview

This repository is the Windows backend for the Schwab BuyLow system.

BuyLow is responsible for automated Schwab-side buy-low execution, Schwab authentication, account and position data, logs, advisory capital readiness, advisory capital utilization, and backend API JSON consumed by dashboard clients.

The Mac `TradingDashboard` frontend should consume backend API JSON. It should not duplicate BuyLow trading logic, Schwab authentication, order sizing, cap math, or capital deployment logic.

Core BuyLow symbols:

- `SPY`
- `QQQ`
- `GLD`

Satellite BuyLow symbols:

- `NVDA`
- `MSFT`
- `AAPL`

Preserve the role distinction: SPY/QQQ/GLD are core symbols; NVDA/MSFT/AAPL are small satellite symbols.

## 2. Backend Responsibilities

This backend owns:

- BuyLow execution logic.
- Schwab authentication and token handling.
- Schwab account, cash, position, quote, and order integration.
- BuyLow config files under `config/`.
- Runtime state and logs under `runtime/` and `C:\temp`.
- Advisory JSON outputs such as:
  - `C:\temp\capital_readiness.json`
  - `C:\temp\capital_utilization.json`
- Dashboard/API endpoints that expose backend JSON to frontend clients.
- Safety checks, diagnostics, budget/cap explanations, and blocked-trade visibility.

Prefer backend changes that produce clear diagnostics, structured JSON, and read-only API endpoints over changes that alter trading behavior.

## 3. Safety Rules

BuyLow execution logic is safety-critical.

Do not:

- Place orders automatically unless the user explicitly requests live order behavior.
- Add or enable hidden order placement.
- Auto-sell `SWVXX`.
- Auto-transfer funds.
- Auto-increase caps.
- Bypass Schwab re-auth.
- Suppress Schwab auth failures in a way that permits trading without valid auth.
- Change SPY/QQQ/GLD core behavior unless explicitly requested.
- Expand satellite risk beyond configured caps unless explicitly requested.
- Make frontend code responsible for trading decisions.

Do:

- Keep suggestions advisory unless explicitly told otherwise.
- Clearly mark advisory outputs with fields such as `manual_actions_only` or `mode: advisory_only`.
- Keep order placement behind existing explicit confirmation paths.
- Prefer dry-run, diagnostics, and JSON reports when uncertain.
- Treat `SWVXX` reserve deployment as manual or BuyLow-eligible only; never forced.

Before code changes:

- Summarize affected files.
- Summarize the intended behavior change.
- Summarize the test plan.

After code changes:

- Report files modified.
- Report validation commands run.
- Report any commands that could not be run and why.

## 4. API Contract Rules

Backend API endpoints should return stable JSON contracts for frontend consumption.

Rules:

- Add fields in a backward-compatible way when possible.
- Do not remove or rename existing response fields without explicit approval.
- Use safe empty states when files are missing or stale.
- Include stale/fresh indicators where useful, such as `is_stale` or `source_stale`.
- Keep all trading decisions on the backend.
- Frontend should display backend status, warnings, suggestions, and manual-action prompts.
- Frontend should not calculate BuyLow eligibility, caps, Schwab auth state, or order sizing.

Current advisory API/file concepts:

- Capital readiness: blocked symbols and suggested manual funding visibility.
- Capital utilization: allocation vs caps, SWVXX reserve, remaining deployment capacity, staged suggestions.

Endpoint additions should be read-only by default.

## 5. Testing Commands

Use PowerShell from the repo root:

```powershell
cd C:\Users\cheng_hamn078\scripts\schwab-buy-low
```

Validate JSON config files:

```powershell
python -m json.tool config\buy.dic
python -m json.tool config\sym_caps.dic
python -m json.tool config\atrk.json
python -m json.tool config\sym_overrides.json
```

Compile touched Python files:

```powershell
python -m py_compile buylow_new.py
python -m py_compile capital_readiness.py
python -m py_compile capital_utilization_engine.py
```

Compile dashboard API when changed:

```powershell
python -m py_compile C:\Users\cheng_hamn078\dashboard\dashboard_api.py
```

Run advisory capital utilization dry-run:

```powershell
python .\capital_utilization_engine.py --dry-run
```

Write advisory capital utilization JSON:

```powershell
python .\capital_utilization_engine.py --write
```

Test dashboard endpoints when the dashboard server is running:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/capital-readiness?k=$env:TRADE_API_KEY"
Invoke-RestMethod "http://127.0.0.1:8000/api/capital/utilization?k=$env:TRADE_API_KEY"
```

Do not run live trading commands unless the user explicitly requests live trading behavior.

## 6. Cross-Machine Workflow

Windows backend:

- Owns Schwab auth, BuyLow execution, positions, logs, advisory JSON, and backend API endpoints.
- Writes local advisory files to `C:\temp`.
- Runs dashboard API on Windows when needed.

Mac frontend:

- Owns Xcode/iPhone UI work.
- Consumes backend JSON/API output.
- Should not duplicate trading logic.
- Should display advisory status and manual-action prompts only.

Recommended workflow:

1. Backend computes and writes JSON on Windows.
2. Dashboard API exposes JSON over HTTP.
3. Mac/iPhone frontend consumes the API response.
4. Frontend displays status, warnings, and suggestions.
5. Any change to trading behavior is made in the backend only, with explicit approval and tests.

When coordinating with the Mac frontend repo, document JSON field names, sample payloads, stale-state behavior, and safety labels. Keep frontend copy clear that funding and deployment actions are suggestions unless the backend explicitly provides a safe manual-action state.
