#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Advisory-only capital utilization diagnostics for BuyLow.

Reads cached positions and BuyLow caps, then writes C:\\temp\\capital_utilization.json.
This module does not call Schwab, place orders, sell SWVXX, transfer funds, or
change BuyLow trading behavior.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any


APP_ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parent)).resolve()
CONFIG_DIR = APP_ROOT / "config"

DEFAULT_POSITIONS_FILE = os.getenv("BUYLOW_POSITIONS_CACHE_FILE", r"C:\temp\positions_cache.json")
DEFAULT_OUTPUT_FILE = os.getenv("BUYLOW_CAPITAL_UTILIZATION_FILE", r"C:\temp\capital_utilization.json")
DEFAULT_SYM_CAPS_FILE = os.getenv("BUYLOW_SYM_CAPS_FILE", str(CONFIG_DIR / "sym_caps.dic"))

BUYLOW_SYMBOLS = ("SPY", "QQQ", "GLD", "NVDA", "MSFT", "AAPL")
CORE_SYMBOLS = ("SPY", "QQQ", "GLD")
SATELLITE_SYMBOLS = ("NVDA", "MSFT", "AAPL")
RESERVE_SYMBOL = "SWVXX"

TARGET_LOW_PCT = 0.30
TARGET_HIGH_PCT = 0.50
MIN_SWVXX_RESERVE_PCT = 0.10


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _round_money(value: Any) -> float:
    return round(_finite_float(value), 2)


def _round_pct(value: Any) -> float:
    return round(_finite_float(value), 4)


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _write_json_atomic(path: str, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def load_positions_cache(path: str = DEFAULT_POSITIONS_FILE) -> dict[str, Any]:
    try:
        data = _read_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_symbol_caps(path: str = DEFAULT_SYM_CAPS_FILE) -> dict[str, float]:
    try:
        raw = _read_json(path)
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in raw.items():
            out[str(key).upper()] = _finite_float(value)
        return out
    except Exception:
        return {}


def _positions_by_symbol(cache: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = cache.get("positions")
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        out[sym] = row
    return out


def _status_for_headroom(current_value: float, cap_value: float) -> str:
    if cap_value <= 0:
        return "under_cap"
    if current_value > cap_value:
        return "over_cap"
    if current_value >= cap_value * 0.90:
        return "near_cap"
    return "under_cap"


def _allocation_weights(symbol_payloads: list[dict[str, Any]], role: str) -> list[tuple[str, float]]:
    candidates = [
        (item["symbol"], _finite_float(item.get("cap_headroom")))
        for item in symbol_payloads
        if item.get("role") == role and _finite_float(item.get("cap_headroom")) > 0
    ]
    total = sum(headroom for _, headroom in candidates)
    if total <= 0:
        return []
    return [(sym, headroom / total) for sym, headroom in candidates]


def _suggest_next_allocation(
    symbol: str,
    role: str,
    cap_headroom: float,
    remaining_to_deploy_low: float,
    remaining_to_deploy_high: float,
) -> float:
    if cap_headroom <= 0:
        return 0.0

    if role == "core":
        pool = max(0.0, remaining_to_deploy_low)
        base_limit = 5000.0
    else:
        pool = max(0.0, remaining_to_deploy_high - max(0.0, remaining_to_deploy_low))
        base_limit = 1000.0

    if pool <= 0:
        return 0.0
    return _round_money(min(cap_headroom, pool, base_limit))


def _build_schedule(
    *,
    remaining_to_deploy_low: float,
    remaining_to_deploy_high: float,
    available_reserve_for_deployment: float,
    symbols: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if available_reserve_for_deployment <= 0 or remaining_to_deploy_high <= 0:
        return []

    core_symbols = [
        item["symbol"] for item in symbols
        if item.get("role") == "core" and _finite_float(item.get("cap_headroom")) > 0
    ]
    satellite_symbols = [
        item["symbol"] for item in symbols
        if item.get("role") == "satellite" and _finite_float(item.get("cap_headroom")) > 0
    ]

    schedule: list[dict[str, Any]] = []
    stage1 = min(max(0.0, remaining_to_deploy_low) * 0.40, available_reserve_for_deployment, 7500.0)
    if stage1 > 0 and core_symbols:
        schedule.append({
            "stage": 1,
            "condition": "only if BuyLow signal eligible",
            "amount": _round_money(stage1),
            "symbols": core_symbols,
            "note": "core deployment",
        })

    remaining_after_stage1 = max(0.0, available_reserve_for_deployment - stage1)
    stage2_need = max(0.0, remaining_to_deploy_low - stage1)
    stage2 = min(stage2_need, remaining_after_stage1, 10000.0)
    if stage2 > 0 and core_symbols:
        schedule.append({
            "stage": 2,
            "condition": "only if BuyLow signal eligible",
            "amount": _round_money(stage2),
            "symbols": core_symbols,
            "note": "continue core deployment toward low target",
        })

    remaining_after_stage2 = max(0.0, remaining_after_stage1 - stage2)
    stage3_need = max(0.0, remaining_to_deploy_high - remaining_to_deploy_low)
    stage3 = min(stage3_need, remaining_after_stage2, 5000.0)
    stage3_symbols = core_symbols + satellite_symbols
    if stage3 > 0 and stage3_symbols:
        schedule.append({
            "stage": 3,
            "condition": "only if BuyLow signal eligible",
            "amount": _round_money(stage3),
            "symbols": stage3_symbols,
            "note": "selective core plus small satellite deployment",
        })

    return schedule


def build_capital_utilization(
    *,
    positions_file: str = DEFAULT_POSITIONS_FILE,
    sym_caps_file: str = DEFAULT_SYM_CAPS_FILE,
    swvxx_cash_reserve: float | None = None,
) -> dict[str, Any]:
    cache = load_positions_cache(positions_file)
    caps = load_symbol_caps(sym_caps_file)
    by_symbol = _positions_by_symbol(cache)

    total_value = _finite_float(
        cache.get("total_account_value"),
        _finite_float((cache.get("balances") or {}).get("total_account_value")),
    )
    asset_total = _finite_float(cache.get("asset_total"), _finite_float((cache.get("summary") or {}).get("market_value")))
    cash_available = _finite_float(cache.get("cash_available"), _finite_float((cache.get("balances") or {}).get("cash_available")))

    swvxx_value = (
        _finite_float(swvxx_cash_reserve)
        if swvxx_cash_reserve is not None
        else _finite_float((by_symbol.get(RESERVE_SYMBOL) or {}).get("market_value"))
    )

    if total_value <= 0:
        total_value = asset_total + cash_available

    current_invested_value = max(0.0, asset_total - swvxx_value)
    current_invested_pct = (current_invested_value / total_value * 100.0) if total_value > 0 else 0.0

    target_deployment_low = total_value * TARGET_LOW_PCT
    target_deployment_high = total_value * TARGET_HIGH_PCT
    remaining_to_deploy_low = max(0.0, target_deployment_low - current_invested_value)
    remaining_to_deploy_high = max(0.0, target_deployment_high - current_invested_value)
    reserve_floor = total_value * MIN_SWVXX_RESERVE_PCT
    available_reserve_for_deployment = max(0.0, swvxx_value - reserve_floor)

    symbols: list[dict[str, Any]] = []
    default_cap = caps.get("DEFAULT", 0.0)
    for sym in BUYLOW_SYMBOLS:
        row = by_symbol.get(sym) or {}
        current_value = _finite_float(row.get("market_value"))
        cap_pct_fraction = caps.get(sym, default_cap)
        cap_value = total_value * cap_pct_fraction if total_value > 0 else 0.0
        cap_headroom = max(0.0, cap_value - current_value)
        role = "core" if sym in CORE_SYMBOLS else "satellite"
        symbols.append({
            "symbol": sym,
            "current_value": _round_money(current_value),
            "current_pct": _round_pct((current_value / total_value * 100.0) if total_value > 0 else 0.0),
            "cap_pct": _round_pct(cap_pct_fraction * 100.0),
            "cap_value": _round_money(cap_value),
            "cap_headroom": _round_money(cap_headroom),
            "role": role,
            "status": _status_for_headroom(current_value, cap_value),
            "suggested_next_allocation": _suggest_next_allocation(
                sym,
                role,
                cap_headroom,
                remaining_to_deploy_low,
                remaining_to_deploy_high,
            ),
        })

    schedule = _build_schedule(
        remaining_to_deploy_low=remaining_to_deploy_low,
        remaining_to_deploy_high=remaining_to_deploy_high,
        available_reserve_for_deployment=available_reserve_for_deployment,
        symbols=symbols,
    )

    other_positions = sorted(
        sym for sym in by_symbol
        if sym not in set(BUYLOW_SYMBOLS) | {RESERVE_SYMBOL}
    )
    warnings: list[str] = []
    if not cache:
        warnings.append(f"Positions cache missing or invalid: {positions_file}")
    if other_positions:
        warnings.append(f"Non-BuyLow positions included in invested value: {', '.join(other_positions)}")
    if swvxx_value <= 0:
        warnings.append("SWVXX reserve not found in positions cache; using 0.")
    if available_reserve_for_deployment <= 0 and swvxx_value > 0:
        warnings.append("SWVXX reserve is at or below suggested reserve floor.")
    warnings.append("Suggestions only; deploy only through BuyLow eligibility, not forced market buys.")

    return {
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": positions_file,
        "source_stale": bool(cache.get("stale", True)),
        "positions_generated_at": cache.get("generated_at") or cache.get("cached_at") or "",
        "swvxx_cash_reserve": _round_money(swvxx_value),
        "cash_available": _round_money(cash_available),
        "account_total_value": _round_money(total_value),
        "current_invested_value": _round_money(current_invested_value),
        "current_invested_pct": _round_pct(current_invested_pct),
        "target_deployment_low": _round_money(target_deployment_low),
        "target_deployment_high": _round_money(target_deployment_high),
        "remaining_to_deploy_low": _round_money(remaining_to_deploy_low),
        "remaining_to_deploy_high": _round_money(remaining_to_deploy_high),
        "symbols": symbols,
        "deployment_schedule": schedule,
        "warnings": warnings,
        "manual_actions_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build advisory-only BuyLow capital utilization JSON.")
    parser.add_argument("--positions-file", default=DEFAULT_POSITIONS_FILE)
    parser.add_argument("--sym-caps-file", default=DEFAULT_SYM_CAPS_FILE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--swvxx-cash-reserve", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Print JSON without writing output file.")
    parser.add_argument("--write", action="store_true", help="Write JSON output file.")
    args = parser.parse_args()

    payload = build_capital_utilization(
        positions_file=args.positions_file,
        sym_caps_file=args.sym_caps_file,
        swvxx_cash_reserve=args.swvxx_cash_reserve,
    )

    if args.write and not args.dry_run:
        _write_json_atomic(args.output, payload)

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
