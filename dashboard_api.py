#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Minimal dashboard API for BuyLow local status files."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_CAPITAL_READINESS_FILE = (
    r"C:\temp\capital_readiness.json"
    if os.name == "nt"
    else "/tmp/capital_readiness.json"
)
CAPITAL_READINESS_FILE = os.getenv(
    "BUYLOW_CAPITAL_READINESS_FILE",
    DEFAULT_CAPITAL_READINESS_FILE,
)


def _empty_capital_readiness() -> dict[str, Any]:
    return {
        "mode": "advisory_only",
        "generated_at": "",
        "schwab_cash_available": 0,
        "schwab_budget_remaining": 0,
        "merrill_reserve_available": 0,
        "merrill_reserve_configured": False,
        "manual_action_required": False,
        "blocked_symbols": [],
        "is_stale": True,
    }


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


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _is_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    current = now.time()
    return dtime(9, 30) <= current <= dtime(16, 0)


def _is_stale(generated_at: str, stale_minutes: int = 30) -> bool:
    generated = _parse_dt(generated_at)
    if generated is None:
        return True
    now = datetime.now(generated.tzinfo) if generated.tzinfo else datetime.now()
    if not _is_market_hours(now):
        return False
    age_sec = (now - generated).total_seconds()
    return age_sec > stale_minutes * 60


def load_capital_readiness(path: str = CAPITAL_READINESS_FILE) -> dict[str, Any]:
    payload = _read_json(path)
    if not payload:
        return _empty_capital_readiness()

    out = _empty_capital_readiness()
    out.update(payload)
    out["mode"] = "advisory_only"
    blocked = out.get("blocked_symbols")
    out["blocked_symbols"] = blocked if isinstance(blocked, list) else []
    out["manual_action_required"] = bool(out.get("manual_action_required"))
    out["merrill_reserve_configured"] = bool(out.get("merrill_reserve_configured"))
    out["is_stale"] = _is_stale(str(out.get("generated_at") or ""))
    return out


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BuyLowDashboardAPI/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/capital-readiness":
            self._send_json(load_capital_readiness())
            return
        if parsed.path == "/health":
            self._send_json({"ok": True})
            return
        self.send_error(404, "Not Found")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[dashboard_api] {self.address_string()} - {fmt % args}")

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="BuyLow local dashboard API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"[dashboard_api] serving http://{args.host}:{args.port}")
    print(f"[dashboard_api] capital readiness file: {CAPITAL_READINESS_FILE}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[dashboard_api] stopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
