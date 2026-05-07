#!/usr/bin/env python3
"""Proposal-only placeholder for the future Trend Rider strategy.

This module intentionally does not place orders. It exists so the project tree
has a stable home for future Trend Rider planning, configuration, and state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Phase 2 path hardening: resolve repo root independently of cwd.
ROOT = Path(__file__).resolve().parents[2]
STRATEGY_DIR = ROOT / "strategies" / "trend_rider"
CONFIG_PATH = STRATEGY_DIR / "trend_config.json"
STATE_PATH = STRATEGY_DIR / "trend_state.json"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def build_proposal() -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    state = load_json(STATE_PATH)
    return {
        "strategy": "trend_rider",
        "mode": "proposal_only",
        "manual_actions_only": True,
        "live_orders_enabled": False,
        "config": config,
        "state": state,
        "suggestions": [],
        "warnings": ["Trend Rider is a placeholder. No live orders are supported."],
    }


def main() -> int:
    print(json.dumps(build_proposal(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
