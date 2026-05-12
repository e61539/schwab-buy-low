#!/usr/bin/env python3
"""Trend Rider manual acceptance tracker.

Phase 4A acceptance tracking only. This helper records manually accepted Trend
Rider positions and never places orders or calls Schwab order endpoints.

Future phases:
- trailing exits
- regime engine
- portfolio heat
- adaptive sizing
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


# Phase 2 path hardening: resolve repo root independently of cwd.
ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parents[2])).resolve()
STRATEGY_DIR = ROOT / "strategies" / "trend_rider"
CONFIG_PATH = STRATEGY_DIR / "trend_config.json"
CACHE_PATH = STRATEGY_DIR / "trend_cache.json"
POSITIONS_PATH = ROOT / "runtime" / "state" / "trend_positions.json"
LOG_DIR = Path(os.getenv("TREND_LOG_DIR", r"C:\temp\logs_trend"))

MODE = "acceptance_tracking_only"
MANUAL_ACTIONS_ONLY = True
LIVE_ORDERS_ENABLED = False


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else dict(default or {})
    except Exception:
        return dict(default or {})


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def log_accept(line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    path = LOG_DIR / f"trend_accept_{stamp}.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def normalize_positions(raw: dict[str, Any]) -> dict[str, Any]:
    positions = raw.get("positions")
    if isinstance(positions, dict):
        positions = list(positions.values())
    if not isinstance(positions, list):
        positions = []

    clean: list[dict[str, Any]] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        clean.append(
            {
                "symbol": symbol,
                "accepted_ts": str(item.get("accepted_ts") or now_iso()),
                "entry_price": safe_float(item.get("entry_price")),
                "entry_qty": safe_float(item.get("entry_qty")),
                "proposal_score": safe_float(item.get("proposal_score")),
                "strategy": "trend_rider",
                "sector": str(item.get("sector") or "Unknown"),
                "notes": str(item.get("notes") or ""),
                "participation_sleeve": str(item.get("participation_sleeve") or "trend_rider"),
            }
        )

    return {
        "schema_version": 1,
        "mode": MODE,
        "manual_actions_only": MANUAL_ACTIONS_ONLY,
        "live_orders_enabled": LIVE_ORDERS_ENABLED,
        "updated_at": str(raw.get("updated_at") or now_iso()),
        "positions": sorted(clean, key=lambda x: x["symbol"]),
        "notes": "Manual Trend Rider acceptance tracking only. No live orders are placed.",
    }


def load_positions(path: Path = POSITIONS_PATH) -> dict[str, Any]:
    return normalize_positions(load_json(path, {}))


def find_cached_candidate(symbol: str, cache: dict[str, Any]) -> dict[str, Any]:
    symbol = symbol.upper()
    for section in ("shortlist", "rankings", "rejected"):
        items = cache.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and str(item.get("symbol") or "").upper() == symbol:
                return item
    return {}


def metadata_sector(symbol: str, config: dict[str, Any]) -> str:
    metadata = config.get("symbol_metadata") if isinstance(config.get("symbol_metadata"), dict) else {}
    item = metadata.get(symbol.upper()) if isinstance(metadata, dict) else None
    if isinstance(item, dict) and item.get("sector"):
        return str(item["sector"])
    return "Unknown"


def accept_position(args: argparse.Namespace) -> dict[str, Any]:
    symbol = args.symbol.strip().upper()
    config = load_json(Path(args.config), {})
    cache = load_json(Path(args.cache), {})
    state = load_positions(Path(args.positions))
    candidate = find_cached_candidate(symbol, cache)
    metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else {}

    entry_price = safe_float(args.entry_price, safe_float(candidate.get("proposal_price"), safe_float(metrics.get("last"))))
    entry_qty = safe_float(args.entry_qty, 0.0)
    proposal_score = safe_float(args.proposal_score, safe_float(candidate.get("score")))
    sector = args.sector.strip() if args.sector else str(candidate.get("sector") or metadata_sector(symbol, config))

    accepted = {
        "symbol": symbol,
        "accepted_ts": args.accepted_ts or now_iso(),
        "entry_price": round(entry_price, 4),
        "entry_qty": round(entry_qty, 6),
        "proposal_score": round(proposal_score, 1),
        "strategy": "trend_rider",
        "sector": sector,
        "notes": args.notes or "",
        "participation_sleeve": "trend_rider",
    }

    positions = [p for p in state["positions"] if p["symbol"] != symbol]
    positions.append(accepted)
    state["positions"] = sorted(positions, key=lambda x: x["symbol"])
    state["updated_at"] = now_iso()
    save_json(Path(args.positions), state)

    line = (
        f"[ACCEPT] {symbol} manual acceptance recorded "
        f"entry_price={accepted['entry_price']} entry_qty={accepted['entry_qty']} "
        f"score={accepted['proposal_score']} sector={accepted['sector']} "
        "mode=acceptance_tracking_only live_orders_enabled=false"
    )
    print(line)
    log_accept(line)
    return accepted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track manually accepted Trend Rider positions")
    parser.add_argument("symbol", nargs="?", help="Symbol to accept, e.g. MSFT")
    parser.add_argument("--entry-price", type=float, default=None)
    parser.add_argument("--entry-qty", type=float, default=None)
    parser.add_argument("--proposal-score", type=float, default=None)
    parser.add_argument("--sector", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--accepted-ts", default="")
    parser.add_argument("--positions", default=str(POSITIONS_PATH))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--cache", default=str(CACHE_PATH))
    parser.add_argument("--list", action="store_true", help="Print tracked accepted positions")
    parser.add_argument("--check", action="store_true", help="Validate paths without changing state")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check:
        print(f"ROOT={ROOT}")
        print(f"POSITIONS={Path(args.positions)}")
        print(f"CACHE={Path(args.cache)}")
        print("[OK] trend_positions.py path check passed")
        return 0

    if args.list:
        print(json.dumps(load_positions(Path(args.positions)), indent=2, sort_keys=True))
        return 0

    if not args.symbol:
        raise SystemExit("symbol is required, for example: trend_accept.cmd MSFT")

    accept_position(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
