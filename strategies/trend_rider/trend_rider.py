#!/usr/bin/env python3
"""Trend Rider proposal engine.

Phase 3 proposal-only engine. This module never places live orders, never calls
Schwab order/confirm endpoints, and does not modify BuyLow or SellHigh logic.

Future phases:
- staged adds after confirmation
- trailing exits and invalidation monitoring
- SellHigh integration for profit-taking
- market regime detection
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


# Phase 2 path hardening: resolve repo root independently of cwd.
ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parents[2])).resolve()
STRATEGY_DIR = ROOT / "strategies" / "trend_rider"
CONFIG_PATH = STRATEGY_DIR / "trend_config.json"
STATE_PATH = STRATEGY_DIR / "trend_state.json"
CACHE_PATH = STRATEGY_DIR / "trend_cache.json"
TREND_POSITIONS_PATH = ROOT / "runtime" / "state" / "trend_positions.json"
POSITIONS_SCRIPT = Path(os.getenv("POSITIONS_SCRIPT", r"C:\Users\cheng_hamn078\dashboard\positions.py"))
POSITIONS_CACHE_PATH = Path(os.getenv("POSITIONS_CACHE_PATH", r"C:\temp\positions_cache.json"))
LOG_DIR = Path(os.getenv("TREND_LOG_DIR", r"C:\temp\logs_trend"))
DEFAULT_DATA_DIR = Path(os.getenv("TREND_DATA_DIR", r"C:\temp"))
DEFAULT_ACCT = os.getenv("TREND_ACCT", os.getenv("TRADE_DEFAULT_ACCT", "IRA1")).strip() or "IRA1"
DEFAULT_TOKENS_FILE = os.getenv("BUYLOW_TOKENS_FILE", r"C:\temp\tokens.txt")
CALLBACK_URL = "https://127.0.0.1"

MODE = "proposal_only"
MANUAL_ACTIONS_ONLY = True
LIVE_ORDERS_ENABLED = False


@dataclass
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Candidate:
    symbol: str
    sector: str
    status: str
    candidate_type: str
    action_hint: str
    next_check: str
    score: float
    proposal_price: float
    proposal_pct: float
    proposal_dollars: float
    invalidation_level: float
    data_as_of: str
    reasons: list[str]
    rejections: list[str]
    reason_codes: list[str]
    metrics: dict[str, Any]


def finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def fnum(value: Any, default: float = 0.0) -> float:
    return float(value) if finite(value) else float(default)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def today_iso() -> str:
    return datetime.now().date().isoformat()


class TeeLogger:
    def __init__(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        self.path = log_dir / f"trend_{stamp}.log"

    def write(self, line: str = "") -> None:
        print(line)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else dict(default or {})
    except Exception:
        return dict(default or {})


def extract_json_payload(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue
    return {}


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def average(values: list[float]) -> float | None:
    clean = [float(v) for v in values if finite(v)]
    if not clean:
        return None
    return sum(clean) / len(clean)


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old


def load_price_csv(symbol: str, data_dir: Path) -> list[Bar]:
    path = data_dir / f"{symbol.upper()}_daily.csv"
    if not path.exists():
        return []

    rows: list[Bar] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for raw in reader:
            if not raw or raw[0] in {"Price", "Ticker", "Date"}:
                continue
            day = parse_date(raw[0])
            if day is None or len(raw) < 5:
                continue
            try:
                rows.append(
                    Bar(
                        day=day,
                        open=float(raw[1]),
                        high=float(raw[2]),
                        low=float(raw[3]),
                        close=float(raw[4]),
                        volume=float(raw[5]) if len(raw) > 5 and finite(raw[5]) else 0.0,
                    )
                )
            except Exception:
                continue
    rows.sort(key=lambda b: b.day)
    return rows


def true_ranges(bars: list[Bar]) -> list[float]:
    out: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        if prev_close is None:
            out.append(bar.high - bar.low)
        else:
            out.append(max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close)))
        prev_close = bar.close
    return out


def recent_swing_low(bars: list[Bar], lookback: int) -> float | None:
    if not bars:
        return None
    recent = bars[-lookback:]
    if not recent:
        return None
    return min(b.low for b in recent)


def days_since(value: str | None) -> int | None:
    d = parse_date(value)
    if d is None:
        return None
    return (datetime.now().date() - d).days


def cooldown_active(symbol: str, state: dict[str, Any], cooldown_days: int) -> tuple[bool, int | None]:
    last = (state.get("last_proposed_symbols") or {}).get(symbol)
    age = days_since(last)
    if age is None:
        return False, None
    return age < cooldown_days, age


def accepted_recently(symbol: str, state: dict[str, Any], recent_days: int) -> tuple[bool, int | None]:
    last = (state.get("last_accepted_symbols") or {}).get(symbol)
    age = days_since(last)
    if age is None:
        return False, None
    return age < recent_days, age


def classify_signal_type(symbol: str, actual_holding_symbols: set[str], accepted_signal_symbols: set[str]) -> str | None:
    if symbol in actual_holding_symbols:
        return "existing_holding"
    if symbol in accepted_signal_symbols:
        return "accepted_signal"
    return None


def add_on_cooldown_active(symbol: str, state: dict[str, Any], cooldown_days: int) -> tuple[bool, int | None]:
    last = (state.get("last_add_on_symbols") or {}).get(symbol)
    age = days_since(last)
    if age is None:
        return False, None
    return age < cooldown_days, age


def defensive_symbols() -> set[str]:
    return {"GLD"}


def choose_action_hint(status: str, reason_codes: list[str], symbol: str = "", sector: str = "") -> str:
    codes = set(reason_codes)
    if "add_on_candidate" in codes:
        return "ADD_ON_CANDIDATE"
    if status == "new_entry":
        return "BUY_CANDIDATE"
    if status == "holding":
        sym = normalize_symbol(symbol)
        if (
            "price_above_sma20" in codes
            and "sma20_above_sma50" in codes
            and "weak_trend_quality" not in codes
            and sym not in defensive_symbols()
        ):
            return "HOLD_STRONG"
        if sym in defensive_symbols() or (sector == "ETF" and "weak_trend_quality" in codes):
            return "HOLD_DEFENSIVE"
        if codes.intersection({"price_below_sma20", "too_far_from_52w_high"}):
            return "HOLD_WEAKENING"
        return "HOLD_STRONG"
    if "cooldown_active" in codes:
        return "WAIT_FOR_COOLDOWN"
    if "overextended" in codes:
        return "WAIT_FOR_PULLBACK"
    if status == "pending_entry":
        return "WATCH_FOR_ENTRY"
    if codes.intersection({"price_below_sma20", "weak_trend_quality", "insufficient_moving_average_history"}):
        return "AVOID_FOR_NOW"
    return "REVIEW_MANUALLY"


def choose_next_check(action_hint: str) -> str:
    if action_hint == "WAIT_FOR_COOLDOWN":
        return "after cooldown expires"
    if action_hint == "WAIT_FOR_PULLBACK":
        return "next daily close near SMA20/pullback zone"
    if action_hint == "WATCH_FOR_ENTRY":
        return "after broker position or manual entry confirmation"
    if action_hint == "AVOID_FOR_NOW":
        return "after price recovers above SMA20 and trend quality improves"
    if action_hint == "ADD_ON_CANDIDATE":
        return "review add-on sizing and caps manually"
    if action_hint == "BUY_CANDIDATE":
        return "manual review before any entry"
    if action_hint in {"HOLD", "HOLD_STRONG"}:
        return "next scheduled trend review"
    if action_hint == "HOLD_WEAKENING":
        return "watch SMA20 recovery and trend invalidation level"
    if action_hint == "HOLD_DEFENSIVE":
        return "monitor defensive allocation and trend quality"
    return "manual review"


def make_action(status: str, reason_codes: list[str], symbol: str = "", sector: str = "") -> tuple[str, str]:
    action_hint = choose_action_hint(status, reason_codes, symbol, sector)
    return action_hint, choose_next_check(action_hint)


def normalize_trend_positions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    positions = raw.get("positions")
    if isinstance(positions, dict):
        positions = list(positions.values())
    if not isinstance(positions, list):
        return []

    out: list[dict[str, Any]] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        out.append(
            {
                "symbol": symbol,
                "accepted_ts": str(item.get("accepted_ts") or ""),
                "entry_price": fnum(item.get("entry_price"), 0.0),
                "entry_qty": fnum(item.get("entry_qty"), 0.0),
                "registry_qty": fnum(item.get("registry_qty", item.get("entry_qty")), 0.0),
                "proposal_score": fnum(item.get("proposal_score"), 0.0),
                "strategy": "trend_rider",
                "sector": str(item.get("sector") or "Unknown"),
                "notes": str(item.get("notes") or ""),
                "participation_sleeve": str(item.get("participation_sleeve") or "trend_rider"),
            }
        )
    return sorted(out, key=lambda x: x["symbol"])


def load_trend_positions(path: Path = TREND_POSITIONS_PATH) -> list[dict[str, Any]]:
    return normalize_trend_positions(load_json(path, {"positions": []}))


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def load_account_hash(acct: str = DEFAULT_ACCT) -> str:
    acct = normalize_symbol(acct)
    for path in (ROOT / "config" / "acct.json", Path(r"C:\temp\acct.json")):
        data = load_json(path, {})
        if isinstance(data, dict):
            value = data.get(acct)
            if value:
                return str(value).strip()
    return ""


def account_digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def last4(value: Any) -> str:
    digits = account_digits(value)
    return digits[-4:] if len(digits) >= 4 else digits


def resolve_linked_account(client: Any, acct: str = DEFAULT_ACCT) -> tuple[str, str, str]:
    configured = load_account_hash(acct)
    wanted = configured or acct
    wanted_digits = account_digits(wanted)
    wanted_last4 = last4(wanted)
    wanted_upper = str(wanted).strip().upper()

    linked_resp = client.account_linked()
    linked = linked_resp.json() if hasattr(linked_resp, "json") else linked_resp
    if not isinstance(linked, list) or not linked:
        raise RuntimeError("No linked accounts returned by Schwab.")

    best: tuple[int, str, str, str] | None = None
    for node in linked:
        if not isinstance(node, dict):
            continue
        account_number = str(node.get("accountNumber") or node.get("accountId") or node.get("number") or "")
        account_hash = str(node.get("hashValue") or node.get("hash") or node.get("accountHash") or "")
        display = str(node.get("displayName") or node.get("description") or account_number)
        if not account_hash:
            continue

        score = 0
        node_digits = account_digits(account_number)
        if wanted_digits and node_digits == wanted_digits:
            score = 100
        elif wanted_digits and last4(node_digits) == wanted_last4:
            score = 80
        elif wanted_upper and display.upper() == wanted_upper:
            score = 60

        if best is None or score > best[0]:
            best = (score, account_hash, account_number, display)

    if best is None or best[0] <= 0:
        raise RuntimeError(f"No linked account matched {acct!r} resolved to {wanted!r}.")
    return best[1], f"{best[2]} {best[3]}".strip(), configured


def fetch_schwab_account_positions(acct: str = DEFAULT_ACCT) -> tuple[Any, dict[str, Any]]:
    debug = {
        "direct_schwab_source_ok": False,
        "direct_schwab_error": "",
        "account": acct,
        "account_config_value": load_account_hash(acct),
        "account_hash_used": "",
        "account_label": "",
    }
    try:
        import schwabdev
    except Exception as exc:
        debug["direct_schwab_error"] = f"Missing schwabdev: {exc}"
        return {}, debug

    app_key = os.getenv("app_key")
    app_secret = os.getenv("app_secret")
    if not app_key or not app_secret:
        debug["direct_schwab_error"] = "Set env vars app_key and app_secret"
        return {}, debug

    try:
        client = schwabdev.Client(app_key, app_secret, CALLBACK_URL, DEFAULT_TOKENS_FILE)
        if hasattr(client, "update_tokens_auto"):
            client.update_tokens_auto()
        elif hasattr(client, "update_tokens"):
            client.update_tokens()
        account_hash, account_label, configured = resolve_linked_account(client, acct)
        debug["account_config_value"] = configured
        debug["account_hash_used"] = account_hash
        debug["account_label"] = account_label
        resp = client.account_details(account_hash, fields="positions")
        payload = resp.json() if hasattr(resp, "json") else resp
        debug["direct_schwab_source_ok"] = True
        return payload, debug
    except Exception as exc:
        debug["direct_schwab_error"] = str(exc)
        return {}, debug


def normalize_broker_positions(raw: Any) -> dict[str, dict[str, float]]:
    if isinstance(raw, dict):
        rows = raw.get("positions", raw.get("securitiesAccount", {}).get("positions", []))
        if isinstance(rows, dict):
            rows = list(rows.values())
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []

    out: dict[str, dict[str, float]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
        symbol = normalize_symbol(row.get("symbol") or instrument.get("symbol"))
        if not symbol:
            continue
        qty = fnum(row.get("qty", row.get("quantity", row.get("longQuantity"))), 0.0)
        if qty > 0:
            market_value = fnum(row.get("market_value", row.get("marketValue")), 0.0)
            cost_basis = fnum(row.get("cost_basis", row.get("costBasis")), 0.0)
            avg_cost = fnum(row.get("avg_cost", row.get("averagePrice", row.get("averageLongPrice"))), 0.0)
            if cost_basis <= 0 and avg_cost > 0:
                cost_basis = avg_cost * qty
            entry = out.setdefault(symbol, {"qty": 0.0, "market_value": 0.0, "cost_basis": 0.0})
            entry["qty"] += qty
            entry["market_value"] += market_value
            entry["cost_basis"] += cost_basis
    for item in out.values():
        item["qty"] = round(fnum(item.get("qty"), 0.0), 6)
        item["market_value"] = round(fnum(item.get("market_value"), 0.0), 2)
        item["cost_basis"] = round(fnum(item.get("cost_basis"), 0.0), 2)
        item["avg_cost"] = round(item["cost_basis"] / item["qty"], 4) if item["qty"] > 0 and item["cost_basis"] > 0 else 0.0
    return out


def run_positions_source(script_path: Path = POSITIONS_SCRIPT) -> tuple[Any, dict[str, Any]]:
    debug = {
        "positions_source": str(script_path),
        "positions_source_exists": script_path.exists(),
        "positions_source_ok": False,
        "positions_source_stale": None,
        "positions_source_error": "",
    }
    if not script_path.exists():
        debug["positions_source_error"] = "positions.py not found"
        return {}, debug

    env = dict(os.environ)
    env.setdefault("SKIP_52W", "1")
    env.setdefault("BUYLOW_HOME", str(ROOT))
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            env=env,
        )
    except Exception as exc:
        debug["positions_source_error"] = str(exc)
        return {}, debug

    payload = extract_json_payload(result.stdout or "")
    if result.returncode != 0:
        debug["positions_source_error"] = (result.stderr or result.stdout or f"positions.py rc={result.returncode}").strip()
        return payload, debug

    if isinstance(payload, dict):
        debug["positions_source_ok"] = payload.get("ok") is not False
        debug["positions_source_stale"] = payload.get("stale")
        if payload.get("source"):
            debug["positions_source"] = f"{script_path} ({payload.get('source')})"
        if payload.get("error"):
            debug["positions_source_error"] = str(payload.get("error"))
    elif isinstance(payload, list):
        debug["positions_source_ok"] = True
    else:
        debug["positions_source_error"] = "positions.py returned invalid JSON payload"
    return payload, debug


def load_positions_cache(path: Path = POSITIONS_CACHE_PATH) -> tuple[Any, dict[str, Any]]:
    debug = {
        "positions_cache_path": str(path),
        "positions_cache_exists": path.exists(),
        "positions_cache_used": False,
        "positions_cache_error": "",
    }
    if not path.exists():
        debug["positions_cache_error"] = "positions cache not found"
        return {}, debug
    payload = load_json(path, {})
    if not payload:
        debug["positions_cache_error"] = "positions cache empty or invalid"
        return {}, debug
    debug["positions_cache_used"] = True
    if isinstance(payload, dict) and payload.get("source"):
        debug["positions_cache_source"] = payload.get("source")
        debug["positions_cache_generated_at"] = payload.get("generated_at") or payload.get("cached_at")
    return payload, debug


def load_broker_position_snapshot(config: dict[str, Any], watchlist: list[str]) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    account_hash = load_account_hash(DEFAULT_ACCT)
    debug: dict[str, Any] = {
        "account": DEFAULT_ACCT,
        "account_hash_used": account_hash,
        "account_config_value": account_hash,
        "positions_loaded_count": 0,
        "symbols_found": [],
        "watchlist_symbols_matched": [],
        "watchlist_symbols_not_matched": [],
    }

    path_value = os.getenv("TREND_BROKER_POSITIONS") or str(config.get("broker_positions_path") or "").strip()
    if path_value:
        path = Path(path_value)
        raw = load_json(path, {}) if path.exists() else {}
        debug["positions_source"] = str(path)
        debug["positions_source_exists"] = path.exists()
        debug["positions_source_ok"] = path.exists()
        debug["positions_source_error"] = "" if path.exists() else "broker_positions_path not found"
    else:
        raw, direct_debug = fetch_schwab_account_positions(DEFAULT_ACCT)
        debug.update(direct_debug)
        if normalize_broker_positions(raw):
            debug["positions_source"] = "direct_schwab_account_details"
            debug["positions_source_ok"] = True
        else:
            raw, source_debug = run_positions_source(POSITIONS_SCRIPT)
            debug.update(source_debug)
            if not normalize_broker_positions(raw):
                cache_raw, cache_debug = load_positions_cache()
                debug.update(cache_debug)
                if normalize_broker_positions(cache_raw):
                    raw = cache_raw
                    debug["positions_source"] = f"{POSITIONS_CACHE_PATH} (cache fallback)"
                    debug["positions_source_ok"] = True

    positions = normalize_broker_positions(raw)
    found = sorted(positions.keys())
    normalized_watchlist = [normalize_symbol(s) for s in watchlist if normalize_symbol(s)]
    matched = [s for s in normalized_watchlist if s in positions and fnum(positions[s].get("qty"), 0.0) > 0]
    not_matched = [s for s in normalized_watchlist if s not in matched]
    debug.update(
        {
            "positions_loaded_count": len(found),
            "symbols_found": found,
            "watchlist_symbols_matched": matched,
            "watchlist_symbols_not_matched": not_matched,
        }
    )
    return positions, debug


def append_broker_only_positions(
    positions: list[dict[str, Any]],
    broker_positions_by_symbol: dict[str, dict[str, float]],
    watchlist: list[str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    existing = {normalize_symbol(p.get("symbol")) for p in positions}
    metadata = config.get("symbol_metadata") if isinstance(config.get("symbol_metadata"), dict) else {}
    out = list(positions)
    for symbol in [normalize_symbol(s) for s in watchlist]:
        broker_position = broker_positions_by_symbol.get(symbol)
        if not broker_position or fnum(broker_position.get("qty"), 0.0) <= 0 or symbol in existing:
            continue
        meta = metadata.get(symbol, {}) if isinstance(metadata.get(symbol), dict) else {}
        out.append(
            {
                "symbol": symbol,
                "accepted_ts": "",
                "entry_price": 0.0,
                "entry_qty": 0.0,
                "registry_qty": 0.0,
                "proposal_score": 0.0,
                "strategy": "trend_rider",
                "sector": str(meta.get("sector") or "Unknown"),
                "notes": "Broker position detected outside Trend Rider accepted-signal registry.",
                "participation_sleeve": "trend_rider",
            }
        )
    return sorted(out, key=lambda x: str(x.get("symbol") or ""))


def score_candidate(
    symbol: str,
    bars: list[Bar],
    config: dict[str, Any],
    state: dict[str, Any],
    selected_sectors: dict[str, int],
    actual_holding_symbols: set[str],
    accepted_signal_symbols: set[str],
) -> Candidate:
    metadata = config.get("symbol_metadata") or {}
    meta = metadata.get(symbol, {})
    sector = str(meta.get("sector") or "Unknown")
    proposal_pct = fnum(config.get("default_proposal_pct"), 0.005)
    account_value = fnum(config.get("account_value_for_sizing"), 100000.0)
    max_data_age_days = int(fnum(config.get("max_data_age_days"), 9999))
    min_bars = int(fnum(config.get("min_bars"), 80))
    cooldown_days = int(fnum(config.get("cooldown_days"), 10))
    recent_purchase_penalty_days = int(fnum(config.get("recent_purchase_penalty_days"), 30))
    sector_caps = config.get("sector_caps") if isinstance(config.get("sector_caps"), dict) else {}
    sector_cap = int(fnum(sector_caps.get(sector), fnum(sector_caps.get("DEFAULT"), 2)))
    liquidity = meta.get("liquidity", {})
    spread_bps = fnum(liquidity.get("spread_bps"), fnum(config.get("default_spread_bps"), 12.0))
    avg_dollar_volume = fnum(liquidity.get("avg_dollar_volume"), 0.0)

    reasons: list[str] = []
    rejections: list[str] = []
    reason_codes: list[str] = []
    metrics: dict[str, Any] = {}
    registry_type = classify_signal_type(symbol, actual_holding_symbols, accepted_signal_symbols)

    if len(bars) < min_bars:
        candidate_type = registry_type or "rejected"
        if candidate_type == "existing_holding":
            status = "holding"
            reason = "active trend position (holding)"
            code = "active_trend_position_holding"
        elif candidate_type == "accepted_signal":
            status = "pending_entry"
            reason = "accepted signal / pending entry"
            code = "accepted_signal_pending_entry"
        else:
            status = "rejected"
            reason = "insufficient price history"
            code = "insufficient_price_history"
        action_hint, next_check = make_action(status, [code], symbol, sector)
        return Candidate(
            symbol,
            sector,
            status,
            candidate_type,
            action_hint,
            next_check,
            0.0,
            0.0,
            proposal_pct,
            account_value * proposal_pct,
            0.0,
            "",
            [reason] if candidate_type in {"existing_holding", "accepted_signal"} else [],
            [] if candidate_type in {"existing_holding", "accepted_signal"} else [reason],
            [code],
            {"bars": len(bars)},
        )

    close_values = [b.close for b in bars]
    latest = bars[-1]
    sma20 = moving_average(close_values, 20)
    sma50 = moving_average(close_values, 50)
    high_52 = max(b.high for b in bars[-252:])
    low_20 = recent_swing_low(bars, 20) or latest.low
    atr14 = average(true_ranges(bars)[-14:]) or 0.0
    atr_pct = atr14 / latest.close if latest.close else 0.0
    daily_returns = [pct_change(close_values[i], close_values[i - 1]) for i in range(1, len(close_values))]
    recent_vol = average([abs(v) for v in daily_returns[-20:]]) or 0.0
    dist_high = (high_52 - latest.close) / high_52 if high_52 else 0.0
    data_age = (datetime.now().date() - latest.day).days
    in_cooldown, cooldown_age = cooldown_active(symbol, state, cooldown_days)
    recent_buy, recent_buy_age = accepted_recently(symbol, state, recent_purchase_penalty_days)

    metrics.update(
        {
            "last": round(latest.close, 4),
            "sma20": round(sma20 or 0.0, 4),
            "sma50": round(sma50 or 0.0, 4),
            "high_52": round(high_52, 4),
            "distance_from_52w_high_pct": round(dist_high * 100.0, 2),
            "atr14": round(atr14, 4),
            "atr_pct": round(atr_pct * 100.0, 2),
            "recent_abs_daily_move_pct": round(recent_vol * 100.0, 2),
            "spread_bps": round(spread_bps, 2),
            "avg_dollar_volume": avg_dollar_volume,
            "data_age_days": data_age,
            "cooldown_age_days": cooldown_age,
            "recent_buy_age_days": recent_buy_age,
        }
    )

    score = 50.0

    if sma20 is None or sma50 is None:
        rejections.append("insufficient moving-average history")
        reason_codes.append("insufficient_moving_average_history")
    elif latest.close > sma20:
        score += 14
        reasons.append("price above SMA20")
        reason_codes.append("price_above_sma20")
    else:
        score -= 30
        rejections.append("price below SMA20")
        reason_codes.append("price_below_sma20")

    if sma20 is not None and sma50 is not None and sma20 > sma50:
        score += 18
        reasons.append("SMA20 above SMA50")
        reason_codes.append("sma20_above_sma50")
    else:
        score -= 28
        rejections.append("weak trend quality")
        reason_codes.append("weak_trend_quality")

    if dist_high < 0.01:
        score -= 8
        rejections.append("overextended")
        reason_codes.append("overextended")
    elif 0.02 <= dist_high <= 0.08:
        score += 16
        reasons.append("moderate pullback")
        reason_codes.append("moderate_pullback")
    elif dist_high <= 0.15:
        score += 6
        reasons.append("constructive pullback")
        reason_codes.append("constructive_pullback")
    else:
        score -= 18
        rejections.append("too far from 52-week high")
        reason_codes.append("too_far_from_52w_high")

    min_avg_dollar_volume = fnum(config.get("min_avg_dollar_volume"), 1000000000.0)
    if avg_dollar_volume >= min_avg_dollar_volume:
        score += 10
        reasons.append("liquid large-cap")
        reason_codes.append("liquid_large_cap")
    else:
        score -= 20
        rejections.append("insufficient liquidity")
        reason_codes.append("insufficient_liquidity")

    max_spread_bps = fnum(config.get("max_spread_bps"), 15.0)
    if spread_bps <= max_spread_bps:
        score += 8
        reasons.append("tight spread")
        reason_codes.append("tight_spread")
    else:
        score -= 15
        rejections.append("poor spread quality")
        reason_codes.append("poor_spread_quality")

    max_atr_pct = fnum(config.get("max_atr_pct"), 0.045)
    if atr_pct > max_atr_pct:
        score -= 18
        rejections.append("volatility penalty")
        reason_codes.append("atr_too_high")
    elif atr_pct > max_atr_pct * 0.70:
        score -= 6
        reasons.append("moderate volatility")
        reason_codes.append("moderate_volatility")

    if recent_vol > fnum(config.get("max_recent_abs_daily_move"), 0.035):
        score -= 8
        rejections.append("volatile recent tape")
        reason_codes.append("volatile_recent_tape")

    if selected_sectors.get(sector, 0) >= sector_cap:
        score -= 20
        rejections.append("sector already represented")
        reason_codes.append("sector_already_represented")

    if in_cooldown:
        score -= 35
        rejections.append("cooldown active")
        reason_codes.append("cooldown_active")

    if recent_buy:
        score -= 18
        rejections.append("recent participation penalty")
        reason_codes.append("recent_participation_penalty")

    if data_age > max_data_age_days:
        score -= 25
        rejections.append("stale market data")
        reason_codes.append("stale_market_data")

    invalidation_candidates = [
        latest.close - 2.0 * atr14 if atr14 > 0 else None,
        sma50,
        low_20,
    ]
    invalidation_values = [v for v in invalidation_candidates if finite(v) and float(v) > 0]
    invalidation = min(invalidation_values) if invalidation_values else latest.close * 0.92
    invalidation = min(invalidation, latest.close * 0.98)

    min_score = fnum(config.get("min_score"), 70.0)
    score = round(clamp(score, 0.0, 100.0), 1)
    candidate_type = registry_type or "new_entry"
    if candidate_type == "existing_holding":
        status = "holding"
    elif candidate_type == "accepted_signal":
        status = "pending_entry"
    else:
        status = "new_entry" if score >= min_score and not rejections else "rejected"
    if status == "new_entry":
        reasons = summarize_reasons(reasons)
    elif status == "holding":
        reasons = ["active trend position (holding)"]
        reason_codes.append("owned")
        reason_codes.append("active_trend_position_holding")
        rejections = []
    elif status == "pending_entry":
        reasons = ["accepted signal / pending entry"]
        reason_codes.append("accepted_signal")
        reason_codes.append("no_broker_qty")
        reason_codes.append("accepted_signal_pending_entry")
        rejections = []
    else:
        candidate_type = "rejected"
    action_hint, next_check = make_action(status, dedupe(reason_codes), symbol, sector)

    return Candidate(
        symbol=symbol,
        sector=sector,
        status=status,
        candidate_type=candidate_type,
        action_hint=action_hint,
        next_check=next_check,
        score=score,
        proposal_price=round(latest.close, 2),
        proposal_pct=proposal_pct,
        proposal_dollars=round(account_value * proposal_pct, 2),
        invalidation_level=round(invalidation, 2),
        data_as_of=latest.day.isoformat(),
        reasons=reasons,
        rejections=dedupe(rejections),
        reason_codes=dedupe(reason_codes),
        metrics=metrics,
    )


def dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def summarize_reasons(reasons: list[str]) -> list[str]:
    clean = dedupe(reasons)
    preferred = ["strong trend", "moderate pullback", "liquid large-cap"]
    out: list[str] = []
    if "price above SMA20" in clean and "SMA20 above SMA50" in clean:
        out.append("strong trend")
    for reason in clean:
        if reason in {"price above SMA20", "SMA20 above SMA50"}:
            continue
        out.append(reason)
    for reason in preferred:
        if reason in out:
            out.remove(reason)
            out.append(reason)
    return out[:5]


def format_proposal(candidate: Candidate) -> str:
    reason_text = ", ".join(candidate.reasons) if candidate.reasons else "trend proposal"
    return (
        f"{candidate.symbol}: proposal near {candidate.proposal_price:.2f}, "
        f"size {candidate.proposal_pct * 100:.2f}% account (${candidate.proposal_dollars:.2f}), "
        f"invalidated below {candidate.invalidation_level:.2f}. "
        f"Score {candidate.score:.1f}, action={candidate.action_hint}. {reason_text}."
    )


def format_ranking(candidate: Candidate) -> str:
    m = candidate.metrics
    reasons = ",".join(candidate.reason_codes)
    return (
        f"{candidate.symbol}: score={candidate.score:.1f}, status={candidate.status}, "
        f"action={candidate.action_hint}, last={m.get('last')}, SMA20={m.get('sma20')}, SMA50={m.get('sma50')}, "
        f"from_high={m.get('distance_from_52w_high_pct')}%, ATR={m.get('atr_pct')}%, "
        f"reasons=[{reasons}], next_check={candidate.next_check}, sector={candidate.sector}, data={candidate.data_as_of}"
    )


def format_rejection(candidate: Candidate) -> str:
    reason = "; ".join(candidate.rejections) if candidate.rejections else "below shortlist threshold"
    return f"{candidate.symbol}: {reason}. Score {candidate.score:.1f}, action={candidate.action_hint}, next={candidate.next_check}."


def format_holding(candidate: Candidate) -> str:
    m = candidate.metrics
    return (
        f"{candidate.symbol}: active trend position (holding). "
        f"Score {candidate.score:.1f}, action={candidate.action_hint}, last={m.get('last')}, SMA20={m.get('sma20')}, "
        f"SMA50={m.get('sma50')}, data={candidate.data_as_of}."
    )


def format_pending_signal(candidate: Candidate) -> str:
    m = candidate.metrics
    return (
        f"{candidate.symbol}: accepted signal / pending entry. "
        f"Score {candidate.score:.1f}, action={candidate.action_hint}, last={m.get('last')}, SMA20={m.get('sma20')}, "
        f"SMA50={m.get('sma50')}, data={candidate.data_as_of}."
    )


def latest_price_map(candidates: list[Candidate]) -> dict[str, float]:
    prices: dict[str, float] = {}
    for candidate in candidates:
        last = candidate.metrics.get("last")
        if finite(last):
            prices[candidate.symbol] = float(last)
        elif finite(candidate.proposal_price):
            prices[candidate.symbol] = float(candidate.proposal_price)
    return prices


def enrich_accepted_positions(
    positions: list[dict[str, Any]],
    prices: dict[str, float],
    broker_positions_by_symbol: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    broker_positions_by_symbol = broker_positions_by_symbol or {}
    for item in positions:
        symbol = normalize_symbol(item.get("symbol"))
        entry_price = fnum(item.get("entry_price"), 0.0)
        registry_qty = fnum(item.get("registry_qty", item.get("entry_qty")), 0.0)
        broker_position = broker_positions_by_symbol.get(symbol, {})
        broker_qty = fnum(broker_position.get("qty"), 0.0)
        broker_market_value = fnum(broker_position.get("market_value"), 0.0)
        broker_avg_cost = fnum(broker_position.get("avg_cost"), 0.0)
        effective_qty = broker_qty if broker_qty > 0 else registry_qty
        if broker_qty > 0:
            position_source = "broker"
        elif registry_qty > 0:
            position_source = "registry"
        else:
            position_source = "accepted_signal_registry"
        current_price = prices.get(symbol, entry_price)
        market_value = broker_market_value if broker_qty > 0 and broker_market_value > 0 else (effective_qty * current_price if effective_qty > 0 else 0.0)
        cost_basis = effective_qty * (broker_avg_cost if broker_qty > 0 and broker_avg_cost > 0 else entry_price) if effective_qty > 0 else 0.0
        gain_dollars = market_value - cost_basis
        gain_pct = (gain_dollars / cost_basis * 100.0) if cost_basis > 0 else None
        out = dict(item)
        out["symbol"] = symbol
        out["broker_qty"] = round(broker_qty, 6)
        out["broker_market_value"] = round(broker_market_value, 2)
        out["broker_avg_cost"] = round(broker_avg_cost, 4)
        out["registry_qty"] = round(registry_qty, 6)
        out["effective_qty"] = round(effective_qty, 6)
        out["position_source"] = position_source
        out["trend_score"] = round(fnum(item.get("proposal_score"), 0.0), 1)
        out["current_price"] = round(current_price, 4) if finite(current_price) else None
        out["market_value"] = round(market_value, 2)
        out["unrealized_gain"] = round(gain_dollars, 2)
        out["unrealized_gain_pct"] = round(gain_pct, 2) if gain_pct is not None else None
        out["participation_sleeve"] = str(item.get("participation_sleeve") or "trend_rider")
        enriched.append(out)
    return enriched


def calculate_trend_exposure(
    actual_positions: list[dict[str, Any]],
    shortlist: list[Candidate],
    account_value: float,
    add_on_opportunities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    total_market_value = sum(fnum(p.get("market_value"), 0.0) for p in actual_positions)
    proposal_dollars = sum(fnum(c.proposal_dollars, 0.0) for c in shortlist)
    add_on_dollars = sum(fnum(c.get("proposal_dollars"), 0.0) for c in (add_on_opportunities or []))
    sector_values: dict[str, float] = {}
    for item in actual_positions:
        sector = str(item.get("sector") or "Unknown")
        sector_values[sector] = sector_values.get(sector, 0.0) + fnum(item.get("market_value"), 0.0)

    denom = account_value if account_value > 0 else 1.0
    return {
        "account_value_for_sizing": round(account_value, 2),
        "participation_sleeve": "trend_rider",
        "total_trend_exposure": round(total_market_value, 2),
        "total_trend_exposure_pct": round(total_market_value / denom * 100.0, 2),
        "proposal_exposure": round(proposal_dollars, 2),
        "proposal_exposure_pct": round(proposal_dollars / denom * 100.0, 2),
        "add_on_proposal_exposure": round(add_on_dollars, 2),
        "add_on_proposal_exposure_pct": round(add_on_dollars / denom * 100.0, 2),
        "sector_exposure": {k: round(v, 2) for k, v in sorted(sector_values.items())},
        "sector_exposure_pct": {k: round(v / denom * 100.0, 2) for k, v in sorted(sector_values.items())},
    }


def build_add_on_opportunities(
    actual_positions: list[dict[str, Any]],
    candidates: list[Candidate],
    config: dict[str, Any],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    by_symbol = {c.symbol: c for c in candidates}
    account_value = fnum(config.get("account_value_for_sizing"), 100000.0)
    add_on_pct = min(
        fnum(config.get("add_on_proposal_pct"), 0.0025),
        fnum(config.get("default_proposal_pct"), 0.005),
    )
    cooldown_days = int(fnum(config.get("add_on_cooldown_days"), 20))
    max_distance = fnum(config.get("add_on_max_distance_above_sma20_pct"), 0.035)
    max_symbol_pct = fnum(config.get("max_symbol_trend_exposure_pct"), 0.015)
    max_total_pct = fnum(config.get("max_total_trend_exposure_pct"), 0.08)
    total_market_value = sum(fnum(p.get("market_value"), 0.0) for p in actual_positions)
    total_cap_dollars = account_value * max_total_pct if account_value > 0 else 0.0

    out: list[dict[str, Any]] = []
    for item in actual_positions:
        symbol = str(item.get("symbol") or "").upper()
        candidate = by_symbol.get(symbol)
        reasons: list[str] = []
        blocked: list[str] = []
        codes: list[str] = ["owned"]
        entry_price = fnum(item.get("entry_price"), 0.0)
        avg_cost = fnum(item.get("broker_avg_cost"), entry_price)
        if avg_cost <= 0:
            avg_cost = entry_price
        current_price = fnum(item.get("current_price"), 0.0)
        effective_qty = fnum(item.get("effective_qty"), 0.0)
        if effective_qty <= 0:
            continue
        market_value = fnum(item.get("market_value"), 0.0)
        sma20 = fnum(candidate.metrics.get("sma20") if candidate else None, 0.0)
        sma50 = fnum(candidate.metrics.get("sma50") if candidate else None, 0.0)
        distance_above_sma20 = (current_price - sma20) / sma20 if sma20 > 0 else None
        cooldown, cooldown_age = add_on_cooldown_active(symbol, state, cooldown_days)

        if current_price > avg_cost > 0:
            reasons.append("profit")
            codes.append("profit")
        else:
            blocked.append("current price not above average cost")
            codes.append("not_profitable")

        if current_price > sma20 > 0:
            reasons.append("price above SMA20")
            codes.append("price_above_sma20")
        else:
            blocked.append("price not above SMA20")
            codes.append("price_not_above_sma20")

        if sma20 > sma50 > 0:
            reasons.append("SMA20 above SMA50")
            codes.append("sma20_above_sma50")
            codes.append("strong_trend")
        else:
            blocked.append("SMA20 not above SMA50")
            codes.append("sma20_not_above_sma50")

        if distance_above_sma20 is not None and distance_above_sma20 <= max_distance:
            reasons.append("not extended above SMA20")
            codes.append("within_sma20_extension_limit")
            codes.append("healthy_extension")
        else:
            blocked.append("too far above SMA20")
            codes.append("too_far_above_sma20")

        if cooldown:
            blocked.append("add-on cooldown active")
            codes.append("add_on_cooldown_active")

        symbol_cap_remaining = (account_value * max_symbol_pct) - market_value if account_value > 0 else 0.0
        total_cap_remaining = total_cap_dollars - total_market_value if total_cap_dollars > 0 else 0.0
        proposal_dollars = min(account_value * add_on_pct, symbol_cap_remaining, total_cap_remaining)
        if proposal_dollars <= 0:
            blocked.append("trend exposure cap reached")
            codes.append("trend_exposure_cap_reached")

        eligible = not blocked and proposal_dollars > 0
        if not eligible:
            continue

        out.append(
            {
                "symbol": symbol,
                "candidate_type": "existing_holding",
                "status": "add_on_opportunity",
                "action_hint": "ADD_ON_CANDIDATE",
                "next_check": choose_next_check("ADD_ON_CANDIDATE"),
                "proposal_price": round(current_price, 2),
                "proposal_pct": round(proposal_dollars / account_value, 6) if account_value > 0 else 0.0,
                "add_on_size": round(proposal_dollars / account_value, 6) if account_value > 0 else 0.0,
                "proposal_dollars": round(proposal_dollars, 2),
                "cooldown_days": cooldown_days,
                "cooldown_age_days": cooldown_age,
                "reasons": summarize_reasons(reasons),
                "rejections": [],
                "reason_codes": dedupe(codes + ["add_on_candidate"]),
                "metrics": {
                    "entry_price": round(entry_price, 4),
                    "avg_cost": round(avg_cost, 4),
                    "current_price": round(current_price, 4),
                    "sma20": round(sma20, 4),
                    "sma50": round(sma50, 4),
                    "distance_above_sma20_pct": round((distance_above_sma20 or 0.0) * 100.0, 2),
                    "symbol_cap_remaining": round(symbol_cap_remaining, 2),
                    "total_cap_remaining": round(total_cap_remaining, 2),
                },
            }
        )
    return sorted(out, key=lambda x: (-fnum(x.get("proposal_dollars"), 0.0), str(x.get("symbol") or "")))


def build_add_on_block_diagnostics(
    actual_positions: list[dict[str, Any]],
    candidates: list[Candidate],
    config: dict[str, Any],
    state: dict[str, Any],
    add_on_opportunities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_symbol = {c.symbol: c for c in candidates}
    eligible_symbols = {normalize_symbol(item.get("symbol")) for item in add_on_opportunities if isinstance(item, dict)}
    account_value = fnum(config.get("account_value_for_sizing"), 100000.0)
    add_on_pct = min(
        fnum(config.get("add_on_proposal_pct"), 0.0025),
        fnum(config.get("default_proposal_pct"), 0.005),
    )
    cooldown_days = int(fnum(config.get("add_on_cooldown_days"), 20))
    max_distance = fnum(config.get("add_on_max_distance_above_sma20_pct"), 0.035)
    max_symbol_pct = fnum(config.get("max_symbol_trend_exposure_pct"), 0.015)
    max_total_pct = fnum(config.get("max_total_trend_exposure_pct"), 0.08)
    total_market_value = sum(fnum(p.get("market_value"), 0.0) for p in actual_positions)
    total_cap_dollars = account_value * max_total_pct if account_value > 0 else 0.0

    out: list[dict[str, Any]] = []
    for item in actual_positions:
        symbol = normalize_symbol(item.get("symbol"))
        if not symbol:
            continue
        candidate = by_symbol.get(symbol)
        reasons: list[str] = []
        broker_qty = fnum(item.get("broker_qty"), 0.0)
        current_price = fnum(item.get("current_price"), 0.0)
        avg_cost = fnum(item.get("broker_avg_cost"), fnum(item.get("entry_price"), 0.0))
        market_value = fnum(item.get("market_value"), 0.0)
        sma20 = fnum(candidate.metrics.get("sma20") if candidate else None, 0.0)
        sma50 = fnum(candidate.metrics.get("sma50") if candidate else None, 0.0)
        distance_above_sma20 = (current_price - sma20) / sma20 if sma20 > 0 else None
        cooldown, cooldown_age = add_on_cooldown_active(symbol, state, cooldown_days)

        if broker_qty <= 0:
            reasons.append("no_broker_qty")
        if current_price <= avg_cost or avg_cost <= 0:
            reasons.append("not_profitable")
        if current_price <= sma20 or sma20 <= 0:
            reasons.append("price_below_sma20")
        if sma20 <= sma50 or sma50 <= 0:
            reasons.append("weak_trend_quality")
        if candidate and "too_far_from_52w_high" in candidate.reason_codes:
            reasons.append("too_far_from_52w_high")
        if candidate and "overextended" in candidate.reason_codes:
            reasons.append("overextended")
        if symbol in defensive_symbols():
            reasons.append("defensive_holding")
        if cooldown:
            reasons.append("cooldown_active")
        if distance_above_sma20 is None or distance_above_sma20 > max_distance:
            reasons.append("extension_above_sma20_limit")

        symbol_cap_remaining = (account_value * max_symbol_pct) - market_value if account_value > 0 else 0.0
        total_cap_remaining = total_cap_dollars - total_market_value if total_cap_dollars > 0 else 0.0
        proposal_dollars = min(account_value * add_on_pct, symbol_cap_remaining, total_cap_remaining)
        if symbol_cap_remaining <= 0 or proposal_dollars <= 0:
            reasons.append("symbol_cap_limit")
        if total_cap_remaining <= 0 or proposal_dollars <= 0:
            reasons.append("sleeve_cap_limit")

        eligible = symbol in eligible_symbols
        if eligible:
            continue
        out.append(
            {
                "symbol": symbol,
                "add_on_eligible": False,
                "add_on_block_reasons": dedupe(reasons or ["review_manually"]),
                "cooldown_age_days": cooldown_age,
                "metrics": {
                    "broker_qty": round(broker_qty, 6),
                    "current_price": round(current_price, 4),
                    "avg_cost": round(avg_cost, 4),
                    "sma20": round(sma20, 4),
                    "sma50": round(sma50, 4),
                    "distance_above_sma20_pct": round((distance_above_sma20 or 0.0) * 100.0, 2),
                    "symbol_cap_remaining": round(symbol_cap_remaining, 2),
                    "total_cap_remaining": round(total_cap_remaining, 2),
                },
            }
        )
    return sorted(out, key=lambda x: str(x.get("symbol") or ""))


def apply_add_on_actions(candidates: list[Candidate], add_on_opportunities: list[dict[str, Any]]) -> list[Candidate]:
    add_on_symbols = {normalize_symbol(item.get("symbol")) for item in add_on_opportunities if isinstance(item, dict)}
    if not add_on_symbols:
        return candidates
    out: list[Candidate] = []
    for candidate in candidates:
        if candidate.status == "holding" and normalize_symbol(candidate.symbol) in add_on_symbols:
            codes = dedupe(candidate.reason_codes + ["add_on_candidate"])
            out.append(
                Candidate(
                    symbol=candidate.symbol,
                    sector=candidate.sector,
                    status=candidate.status,
                    candidate_type=candidate.candidate_type,
                    action_hint="ADD_ON_CANDIDATE",
                    next_check=choose_next_check("ADD_ON_CANDIDATE"),
                    score=candidate.score,
                    proposal_price=candidate.proposal_price,
                    proposal_pct=candidate.proposal_pct,
                    proposal_dollars=candidate.proposal_dollars,
                    invalidation_level=candidate.invalidation_level,
                    data_as_of=candidate.data_as_of,
                    reasons=candidate.reasons,
                    rejections=candidate.rejections,
                    reason_codes=codes,
                    metrics=candidate.metrics,
                )
            )
        else:
            out.append(candidate)
    return out


def build_action_summary(candidates: list[Candidate]) -> dict[str, list[str]]:
    order = [
        "ADD_ON_CANDIDATE",
        "BUY_CANDIDATE",
        "HOLD_STRONG",
        "HOLD_WEAKENING",
        "HOLD_DEFENSIVE",
        "HOLD",
        "WAIT_FOR_COOLDOWN",
        "WAIT_FOR_PULLBACK",
        "WATCH_FOR_ENTRY",
        "AVOID_FOR_NOW",
        "REVIEW_MANUALLY",
    ]
    summary: dict[str, list[str]] = {key: [] for key in order}
    for candidate in candidates:
        summary.setdefault(candidate.action_hint, []).append(candidate.symbol)
    return {key: sorted(values) for key, values in summary.items() if values}


def render_action_plan(report: dict[str, Any], logger: TeeLogger) -> None:
    summary = report.get("action_summary") if isinstance(report.get("action_summary"), dict) else {}
    guidance = {
        "HOLD_WEAKENING": "watch SMA20 recovery",
        "WAIT_FOR_PULLBACK": "wait for pullback toward SMA20",
        "WAIT_FOR_COOLDOWN": "re-evaluate after cooldown expiry",
        "AVOID_FOR_NOW": "below SMA20 / weak trend",
    }
    order = [
        ("HOLD_STRONG", summary.get("HOLD_STRONG") or []),
        ("HOLD_DEFENSIVE", summary.get("HOLD_DEFENSIVE") or []),
        ("HOLD_WEAKENING", summary.get("HOLD_WEAKENING") or []),
        ("WAIT_FOR_PULLBACK", summary.get("WAIT_FOR_PULLBACK") or []),
        ("WAIT_FOR_COOLDOWN", summary.get("WAIT_FOR_COOLDOWN") or []),
        ("AVOID_FOR_NOW", summary.get("AVOID_FOR_NOW") or []),
    ]

    logger.write("")
    logger.write("ACTION PLAN")
    wrote_any = False
    for label, symbols in order:
        if not symbols:
            continue
        if wrote_any:
            logger.write("")
        logger.write(label)
        for symbol in symbols:
            logger.write(f"* {symbol}")
        if label in guidance:
            logger.write(f"  -> {guidance[label]}")
        wrote_any = True

    add_on_symbols = [
        normalize_symbol(item.get("symbol"))
        for item in report.get("add_on_opportunities", [])
        if isinstance(item, dict) and normalize_symbol(item.get("symbol"))
    ]
    if wrote_any:
        logger.write("")
    logger.write("ADD-ON CANDIDATES")
    if add_on_symbols:
        for symbol in sorted(add_on_symbols):
            logger.write(f"* {symbol}")
    else:
        logger.write("* None")

    new_buy_symbols = [
        normalize_symbol(item.get("symbol"))
        for item in report.get("new_entries", report.get("shortlist", []))
        if isinstance(item, dict) and normalize_symbol(item.get("symbol"))
    ]
    logger.write("")
    logger.write("NEW BUY CANDIDATES")
    if new_buy_symbols:
        for symbol in sorted(new_buy_symbols):
            logger.write(f"* {symbol}")
    else:
        logger.write("* None")


def build_report(
    config: dict[str, Any],
    state: dict[str, Any],
    data_dir: Path,
    trend_positions_path: Path = TREND_POSITIONS_PATH,
) -> dict[str, Any]:
    watchlist = [str(s).upper() for s in config.get("watchlist", [])]
    max_proposals = int(fnum(config.get("max_proposals"), 3))
    account_value = fnum(config.get("account_value_for_sizing"), 100000.0)
    accepted_raw = load_trend_positions(trend_positions_path)
    broker_positions_by_symbol, broker_debug = load_broker_position_snapshot(config, watchlist)
    accepted_raw = append_broker_only_positions(accepted_raw, broker_positions_by_symbol, watchlist, config)
    accepted_positions = enrich_accepted_positions(accepted_raw, {}, broker_positions_by_symbol)
    actual_positions = [p for p in accepted_positions if fnum(p.get("effective_qty"), 0.0) > 0]
    actual_holding_symbols = {
        normalize_symbol(p.get("symbol"))
        for p in actual_positions
        if fnum(p.get("broker_qty"), 0.0) > 0
    }
    accepted_signal_symbols = {
        normalize_symbol(p.get("symbol"))
        for p in accepted_positions
        if normalize_symbol(p.get("symbol")) and fnum(p.get("effective_qty"), 0.0) <= 0
    }
    selected_sectors: dict[str, int] = {}
    candidates: list[Candidate] = []

    for symbol in watchlist:
        bars = load_price_csv(symbol, data_dir)
        candidate = score_candidate(symbol, bars, config, state, selected_sectors, actual_holding_symbols, accepted_signal_symbols)
        candidates.append(candidate)
        if candidate.status == "new_entry":
            selected_sectors[candidate.sector] = selected_sectors.get(candidate.sector, 0) + 1

    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    shortlist = [c for c in ranked if c.status == "new_entry"][:max_proposals]
    shortlist_symbols = {c.symbol for c in shortlist}
    accepted_positions = enrich_accepted_positions(accepted_raw, latest_price_map(candidates), broker_positions_by_symbol)
    actual_positions = [p for p in accepted_positions if fnum(p.get("effective_qty"), 0.0) > 0]
    add_on_opportunities = build_add_on_opportunities(actual_positions, candidates, config, state)
    add_on_blocked = build_add_on_block_diagnostics(actual_positions, candidates, config, state, add_on_opportunities)
    ranked = apply_add_on_actions(ranked, add_on_opportunities)
    shortlist = apply_add_on_actions(shortlist, add_on_opportunities)
    existing_holdings = [c for c in ranked if c.candidate_type == "existing_holding"]
    pending_signals = [c for c in ranked if c.candidate_type == "accepted_signal"]
    rejected = [c for c in ranked if c.symbol not in shortlist_symbols and c.candidate_type == "rejected"]
    trend_sleeve = calculate_trend_exposure(actual_positions, shortlist, account_value, add_on_opportunities)

    return {
        "strategy": "trend_rider",
        "mode": MODE,
        "manual_actions_only": MANUAL_ACTIONS_ONLY,
        "live_orders_enabled": LIVE_ORDERS_ENABLED,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "log_dir": str(LOG_DIR),
        "trend_positions_path": str(trend_positions_path),
        "shortlist": [asdict(c) for c in shortlist],
        "new_entries": [asdict(c) for c in shortlist],
        "add_on_opportunities": add_on_opportunities,
        "add_on_blocked": add_on_blocked,
        "pending_signals": [asdict(c) for c in pending_signals],
        "existing_holdings": [asdict(c) for c in existing_holdings],
        "rankings": [asdict(c) for c in ranked],
        "rejected": [asdict(c) for c in rejected],
        "action_summary": build_action_summary(ranked),
        "accepted_positions": accepted_positions,
        "actual_positions": actual_positions,
        "broker_position_debug": broker_debug,
        "trend_sleeve": trend_sleeve,
        "warnings": [
            "Proposal-only output. No live orders are placed.",
            "Accepted signal registry records are not holdings unless effective_qty is greater than zero.",
            "Local CSV data may be stale unless refreshed externally.",
        ],
    }


def render_report(report: dict[str, Any], logger: TeeLogger) -> None:
    logger.write("NEW ENTRY CANDIDATES")
    shortlist = [Candidate(**c) for c in report.get("new_entries", report.get("shortlist", []))]
    if not shortlist:
        logger.write("No proposal-qualified trend entries today.")
    for candidate in shortlist:
        logger.write(format_proposal(candidate))

    logger.write("")
    logger.write("ADD-ON OPPORTUNITIES")
    add_ons = report.get("add_on_opportunities", [])
    if not add_ons:
        logger.write("None.")
    for item in add_ons:
        reasons = ", ".join(item.get("reasons") or ["conservative momentum add-on"])
        logger.write(
            f"{item.get('symbol')}: add-on near {fnum(item.get('proposal_price'), 0.0):.2f}, "
            f"action={item.get('action_hint')}, add_on_size={fnum(item.get('add_on_size', item.get('proposal_pct')), 0.0) * 100:.2f}%, "
            f"size ${fnum(item.get('proposal_dollars'), 0.0):.2f}, reasons={item.get('reason_codes', [])}. {reasons}."
        )

    logger.write("")
    logger.write("ADD-ON BLOCKED")
    blocked_add_ons = report.get("add_on_blocked", [])
    if not blocked_add_ons:
        logger.write("None.")
    for item in blocked_add_ons:
        logger.write(
            f"{item.get('symbol')}: blocked, "
            f"reasons={item.get('add_on_block_reasons', [])}"
        )

    logger.write("")
    logger.write("PENDING / ACCEPTED SIGNALS")
    pending = [Candidate(**c) for c in report.get("pending_signals", [])]
    accepted_by_symbol = {
        str(item.get("symbol") or "").upper(): item
        for item in report.get("accepted_positions", [])
        if isinstance(item, dict)
    }
    if not pending:
        logger.write("None.")
    for candidate in pending:
        logger.write(format_pending_signal(candidate))
        item = accepted_by_symbol.get(candidate.symbol, {})
        logger.write(
            f"{candidate.symbol}: accepted={item.get('accepted_ts')}, "
            f"broker_qty={item.get('broker_qty')}, registry_qty={item.get('registry_qty')}, "
            f"effective_qty={item.get('effective_qty')}, source={item.get('position_source')}"
        )

    logger.write("")
    logger.write("EXISTING HOLDINGS")
    holdings = [Candidate(**c) for c in report.get("existing_holdings", [])]
    if not holdings:
        logger.write("None.")
    for candidate in holdings:
        logger.write(format_holding(candidate))
        item = accepted_by_symbol.get(candidate.symbol, {})
        gain = item.get("unrealized_gain_pct")
        gain_text = "n/a" if gain is None else f"{gain:.2f}%"
        logger.write(
            f"{candidate.symbol}: strategy={item.get('strategy')}, "
            f"sleeve={item.get('participation_sleeve')}, score={item.get('trend_score')}, "
            f"accepted={item.get('accepted_ts')}, qty={item.get('entry_qty')}, "
            f"broker_qty={item.get('broker_qty')}, registry_qty={item.get('registry_qty')}, "
            f"effective_qty={item.get('effective_qty')}, source={item.get('position_source')}, "
            f"entry={item.get('entry_price')}, current={item.get('current_price')}, "
            f"unrealized={gain_text}"
        )

    logger.write("")
    logger.write("REJECTED")
    rejected = [Candidate(**c) for c in report.get("rejected", [])]
    if not rejected:
        logger.write("None.")
    for candidate in rejected:
        logger.write(format_rejection(candidate))

    logger.write("")
    logger.write("ACTION SUMMARY")
    summary = report.get("action_summary") if isinstance(report.get("action_summary"), dict) else {}
    if not summary:
        logger.write("None.")
    for action in [
        "ADD_ON_CANDIDATE",
        "BUY_CANDIDATE",
        "HOLD_STRONG",
        "HOLD_WEAKENING",
        "HOLD_DEFENSIVE",
        "HOLD",
        "WAIT_FOR_COOLDOWN",
        "WAIT_FOR_PULLBACK",
        "WATCH_FOR_ENTRY",
        "AVOID_FOR_NOW",
        "REVIEW_MANUALLY",
    ]:
        symbols = summary.get(action) or []
        if not symbols:
            continue
        logger.write(f"{action}:")
        for symbol in symbols:
            logger.write(f"- {symbol}")

    logger.write("")
    logger.write("WATCHLIST RANKINGS")
    rankings = [Candidate(**c) for c in report.get("rankings", [])]
    for candidate in rankings:
        logger.write(format_ranking(candidate))

    debug = report.get("broker_position_debug") if isinstance(report.get("broker_position_debug"), dict) else {}
    logger.write("")
    logger.write("BROKER POSITION DEBUG")
    logger.write(
        f"account={debug.get('account', '')}, "
        f"account_config_value={debug.get('account_config_value', '')}, "
        f"account_hash_used={debug.get('account_hash_used', '')}"
    )
    if debug.get("account_label"):
        logger.write(f"account_label={debug.get('account_label')}")
    logger.write(f"positions_source={debug.get('positions_source', '')}")
    if debug.get("direct_schwab_error"):
        logger.write(f"direct_schwab_error={debug.get('direct_schwab_error')}")
    if debug.get("positions_source_error"):
        logger.write(f"positions_source_error={debug.get('positions_source_error')}")
    if debug.get("positions_cache_used"):
        logger.write(
            f"positions_cache_used={debug.get('positions_cache_used')}, "
            f"positions_cache_generated_at={debug.get('positions_cache_generated_at', '')}"
        )
    logger.write(f"positions_loaded_count={debug.get('positions_loaded_count', 0)}")
    logger.write(f"symbols_found={debug.get('symbols_found', [])}")
    logger.write(f"watchlist_symbols_matched={debug.get('watchlist_symbols_matched', [])}")
    logger.write(f"watchlist_symbols_not_matched={debug.get('watchlist_symbols_not_matched', [])}")

    sleeve = report.get("trend_sleeve") if isinstance(report.get("trend_sleeve"), dict) else {}
    logger.write("")
    logger.write("TREND SLEEVE EXPOSURE")
    logger.write(
        f"total={sleeve.get('total_trend_exposure_pct', 0.0)}%, "
        f"proposal={sleeve.get('proposal_exposure_pct', 0.0)}%, "
        f"add_on={sleeve.get('add_on_proposal_exposure_pct', 0.0)}%, "
        f"sector={sleeve.get('sector_exposure_pct', {})}"
    )

    render_action_plan(report, logger)


def update_state(state: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    state = dict(state)
    state["mode"] = MODE
    state["manual_actions_only"] = MANUAL_ACTIONS_ONLY
    state["live_orders_enabled"] = LIVE_ORDERS_ENABLED
    state["last_evaluation"] = report.get("generated_at")
    state.setdefault("last_proposed_symbols", {})
    state.setdefault("last_accepted_symbols", {})
    today = today_iso()
    for item in report.get("accepted_positions", []):
        sym = item.get("symbol")
        accepted_ts = item.get("accepted_ts")
        if sym and accepted_ts:
            state["last_accepted_symbols"][sym] = str(accepted_ts)[:10]
    for item in report.get("shortlist", []):
        sym = item.get("symbol")
        if sym:
            state["last_proposed_symbols"][sym] = today
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trend Rider proposal-only engine")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--cache", default=str(CACHE_PATH))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--trend-positions", default=str(TREND_POSITIONS_PATH))
    parser.add_argument("--no-write-state", action="store_true", help="Do not update trend_state.json")
    parser.add_argument("--json", action="store_true", help="Print JSON report after text output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    state_path = Path(args.state)
    cache_path = Path(args.cache)
    data_dir = Path(args.data_dir)
    trend_positions_path = Path(args.trend_positions)

    config = load_json(config_path)
    state = load_json(state_path, {"mode": MODE, "last_proposed_symbols": {}, "last_accepted_symbols": {}})
    logger = TeeLogger(LOG_DIR)

    report = build_report(config, state, data_dir, trend_positions_path)
    render_report(report, logger)
    save_json(cache_path, report)
    if not args.no_write_state:
        save_json(state_path, update_state(state, report))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
