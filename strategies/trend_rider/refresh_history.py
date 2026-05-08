#!/usr/bin/env python3
"""Refresh Trend Rider daily history CSV files.

Proposal infrastructure only. This script fetches Schwab market-data price
history and writes C:\\temp\\SYMBOL_daily.csv files. It never places orders,
never calls preview/confirm endpoints, and does not modify BuyLow/SellHigh.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Phase 2 path hardening: resolve repo root independently of cwd.
ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parents[2])).resolve()
STRATEGY_DIR = ROOT / "strategies" / "trend_rider"
CONFIG_PATH = STRATEGY_DIR / "trend_config.json"
DEFAULT_DATA_DIR = Path(os.getenv("TREND_DATA_DIR", r"C:\temp"))
DEFAULT_TOKENS_FILE = os.getenv("BUYLOW_TOKENS_FILE", r"C:\temp\tokens.txt")
CALLBACK_URL = "https://127.0.0.1"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def parse_symbols(raw: str | None, config_path: Path) -> list[str]:
    if raw:
        symbols = raw.replace(",", " ").split()
    else:
        cfg = load_json(config_path)
        symbols = [str(s) for s in cfg.get("watchlist", [])]
    out: list[str] = []
    seen: set[str] = set()
    for sym in symbols:
        clean = sym.strip().upper()
        if clean and clean not in seen:
            out.append(clean)
            seen.add(clean)
    return out


def parse_json_response(resp_or_text: Any) -> Any:
    if hasattr(resp_or_text, "json"):
        return resp_or_text.json()
    return json.loads(getattr(resp_or_text, "text", resp_or_text))


def new_schwab_client(tokens_file: str):
    try:
        import schwabdev
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'schwabdev'. Use the Python environment that runs SellHigh/BuyLow.") from exc

    app_key = os.getenv("app_key")
    app_secret = os.getenv("app_secret")
    if not app_key or not app_secret:
        raise RuntimeError("Set env vars app_key/app_secret before refreshing Trend Rider history.")

    client = schwabdev.Client(app_key, app_secret, CALLBACK_URL, tokens_file)
    try:
        client.update_tokens()
    except Exception:
        pass
    return client


def fetch_history(client: Any, symbol: str, years: int) -> list[dict[str, Any]]:
    try:
        data = parse_json_response(
            client.price_history(
                symbol,
                period_type="year",
                period=years,
                frequency_type="daily",
                frequency=1,
            )
        )
    except Exception:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("Missing dependency 'requests'.") from exc

        url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {client.access_token}"}
        params = {
            "symbol": symbol,
            "periodType": "year",
            "period": str(years),
            "frequencyType": "daily",
            "frequency": "1",
            "needExtendedHoursData": "false",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=(10, 30))
        if resp.status_code in (401, 403):
            raise RuntimeError(f"Schwab history auth failed with HTTP {resp.status_code}")
        resp.raise_for_status()
        data = parse_json_response(resp)

    candles = (data or {}).get("candles") or []
    rows: list[dict[str, Any]] = []
    for candle in candles:
        try:
            day = datetime.fromtimestamp(float(candle["datetime"]) / 1000, tz=timezone.utc).date().isoformat()
            rows.append(
                {
                    "date": day,
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                }
            )
        except Exception:
            continue
    rows.sort(key=lambda r: str(r["date"]))
    return rows


def write_history_csv(symbol: str, rows: list[dict[str, Any]], data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{symbol.upper()}_daily.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Price", "Open", "High", "Low", "Close"])
        writer.writerow(["Ticker", symbol.upper(), symbol.upper(), symbol.upper(), symbol.upper()])
        writer.writerow(["Date", "", "", "", ""])
        for row in rows:
            writer.writerow(
                [
                    row["date"],
                    f"{float(row['open']):.4f}",
                    f"{float(row['high']):.4f}",
                    f"{float(row['low']):.4f}",
                    f"{float(row['close']):.4f}",
                ]
            )
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh Trend Rider daily CSV history. Market data only; no orders.")
    parser.add_argument("--symbols", help="Comma or space separated symbols. Defaults to trend_config.json watchlist.")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--tokens-file", default=DEFAULT_TOKENS_FILE)
    parser.add_argument("--years", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report without writing CSV files.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = parse_symbols(args.symbols, Path(args.config))
    if not symbols:
        print("[ERR] no symbols supplied and no watchlist found")
        return 2

    print("[INFO] Trend Rider history refresh is market-data only. No orders will be placed.")
    print(f"[INFO] symbols={','.join(symbols)} years={args.years} data_dir={args.data_dir}")
    client = new_schwab_client(args.tokens_file)
    data_dir = Path(args.data_dir)

    failures = 0
    for symbol in symbols:
        try:
            rows = fetch_history(client, symbol, args.years)
            if not rows:
                raise RuntimeError("no daily candles returned")
            if args.dry_run:
                print(f"[DRY] {symbol}: {len(rows)} rows {rows[0]['date']}..{rows[-1]['date']}")
            else:
                path = write_history_csv(symbol, rows, data_dir)
                print(f"[OK] {symbol}: wrote {len(rows)} rows to {path}")
        except Exception as exc:
            failures += 1
            print(f"[ERR] {symbol}: {exc}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
