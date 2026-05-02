#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Advisory-only capital readiness output for BuyLow.

This module reads manually maintained Merrill reserve data and writes
C:\\temp\\capital_readiness.json. It never calls Merrill APIs, transfers money,
places trades, or changes BuyLow caps.
"""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APP_ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parent)).resolve()
CONFIG_DIR = APP_ROOT / "config"
try:
    TZ = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    TZ = datetime.now().astimezone().tzinfo

DEFAULT_MERRILL_RESERVE_FILE = os.getenv(
    "BUYLOW_MERRILL_RESERVE_FILE",
    str(CONFIG_DIR / "merrill_reserve.json"),
)
DEFAULT_OUTPUT_FILE = os.getenv(
    "BUYLOW_CAPITAL_READINESS_FILE",
    r"C:\temp\capital_readiness.json",
)


@contextmanager
def _file_lock(path: str, timeout_sec: float = 5.0):
    lock_path = str(Path(path).with_suffix(Path(path).suffix + ".lock"))
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    fh = open(lock_path, "a+b")
    try:
        while True:
            try:
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except ImportError:
                break
            except Exception:
                if time.time() - start >= timeout_sec:
                    raise TimeoutError(f"Timed out waiting for lock: {lock_path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                pass
            except Exception:
                pass
    finally:
        fh.close()


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


def _read_json(path: str) -> dict[str, Any]:
    try:
        p = Path(path)
        if not p.exists():
            return {}
        with p.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: str, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def _available_merrill_holdings(reserve: dict[str, Any]) -> list[dict[str, Any]]:
    holdings = reserve.get("holdings")
    if not isinstance(holdings, list):
        holdings = []

    out: list[dict[str, Any]] = []
    cash_available = _finite_float(reserve.get("cash_available"))
    if cash_available > 0:
        out.append({
            "symbol": "Cash",
            "market_value": cash_available,
            "priority": -1,
        })

    for item in holdings:
        if not isinstance(item, dict):
            continue
        if item.get("available_for_funding") is False:
            continue
        mv = _finite_float(item.get("market_value"))
        if mv <= 0:
            continue
        out.append({
            "symbol": str(item.get("symbol") or "UNKNOWN"),
            "market_value": mv,
            "priority": int(_finite_float(item.get("priority"), 99)),
        })

    return sorted(out, key=lambda x: (x["priority"], x["symbol"]))


def load_merrill_reserve(path: str = DEFAULT_MERRILL_RESERVE_FILE) -> dict[str, Any]:
    reserve = _read_json(path)
    holdings = _available_merrill_holdings(reserve)
    available = sum(_finite_float(h.get("market_value")) for h in holdings)
    source = holdings[0] if holdings else {}

    return {
        "configured": bool(reserve),
        "path": path,
        "as_of": reserve.get("as_of") or "",
        "account_label": reserve.get("account_label") or "Merrill Edge Reserve",
        "reserve_available": _round_money(available),
        "suggested_source_holding": source.get("symbol") or "",
    }


def build_capital_readiness(
    *,
    symbol: str,
    block_reason: str,
    schwab_cash_available: float,
    schwab_budget_remaining: float,
    target_price: float,
    current_price: float,
    suggested_funding_needed: float,
    manual_action_required: bool,
    merrill_reserve_file: str = DEFAULT_MERRILL_RESERVE_FILE,
) -> dict[str, Any]:
    merrill = load_merrill_reserve(merrill_reserve_file)
    reserve_available = _finite_float(merrill.get("reserve_available"))
    funding_needed = _round_money(max(0.0, _finite_float(suggested_funding_needed)))
    current = _finite_float(current_price)
    target = _finite_float(target_price)
    distance_to_target_pct = ((current / target) - 1.0) * 100.0 if current > 0 and target > 0 else 0.0

    source_holding = merrill.get("suggested_source_holding") or ""
    blocked_symbols = []
    if symbol:
        blocked_symbols.append({
            "symbol": symbol.upper(),
            "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "block_reason": block_reason,
            "target_price": _round_money(target),
            "current_price": _round_money(current),
            "distance_to_target_pct": _round_pct(distance_to_target_pct),
            "suggested_funding_needed": funding_needed,
            "suggested_source_account": merrill.get("account_label") or "Merrill Edge Reserve",
            "suggested_source_holding": source_holding,
            "manual_action_required": bool(manual_action_required),
        })

    return {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "mode": "advisory_only",
        "schwab_cash_available": _round_money(schwab_cash_available),
        "schwab_budget_remaining": _round_money(schwab_budget_remaining),
        "merrill_reserve_available": _round_money(reserve_available),
        "merrill_reserve_configured": bool(merrill.get("configured")),
        "merrill_reserve_file": merrill.get("path") or merrill_reserve_file,
        "merrill_reserve_as_of": merrill.get("as_of") or "",
        "manual_action_required": bool(manual_action_required),
        "blocked_symbols": blocked_symbols,
    }


def write_capital_readiness(
    *,
    output_file: str = DEFAULT_OUTPUT_FILE,
    **kwargs: Any,
) -> dict[str, Any]:
    payload = build_capital_readiness(**kwargs)
    with _file_lock(output_file):
        existing = _read_json(output_file)
        existing_symbols = existing.get("blocked_symbols") if isinstance(existing, dict) else []
        if isinstance(existing_symbols, list) and payload.get("blocked_symbols"):
            merged: dict[str, dict[str, Any]] = {}
            for item in existing_symbols:
                if isinstance(item, dict) and item.get("symbol"):
                    merged[str(item["symbol"]).upper()] = item
            for item in payload["blocked_symbols"]:
                if isinstance(item, dict) and item.get("symbol"):
                    merged[str(item["symbol"]).upper()] = item
            payload["blocked_symbols"] = list(merged.values())
            payload["manual_action_required"] = any(
                bool(item.get("manual_action_required"))
                for item in payload["blocked_symbols"]
                if isinstance(item, dict)
            )
        _write_json_atomic(output_file, payload)
    return payload
