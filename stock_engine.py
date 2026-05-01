#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone stock selection engine for BuyLow expansion candidates.

This script does not place orders and does not modify BuyLow trading logic.
It writes advisory output to C:\\temp\\stock_candidates.json and, when not in
dry-run mode, writes the only file BuyLow should consume for expanded symbols:
C:\\temp\\approved_symbols.txt.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any, Iterable

CALLBACK_URL = "https://127.0.0.1"
DEFAULT_OUTPUT = r"C:\temp\stock_candidates.json"
DEFAULT_APPROVED = r"C:\temp\approved_symbols.txt"
DEFAULT_LOG = r"C:\temp\stock_engine.log"
DEFAULT_TOKENS_FILE = os.getenv("BUYLOW_TOKENS_FILE", r"C:\temp\tokens.txt")

DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "AVGO",
    "TSLA", "JPM", "LLY", "V", "MA", "XOM", "UNH", "COST", "HD",
    "PG", "JNJ", "WMT", "BAC", "ABBV", "KO", "MRK", "CVX", "CRM",
    "ORCL", "AMD", "NFLX", "ADBE", "CSCO", "PEP", "TMO", "MCD",
    "ABT", "LIN", "DIS", "ACN", "QCOM", "TXN", "IBM", "AMAT",
    "NOW", "INTU", "ISRG", "CAT", "GS", "MS", "NEE", "PM", "HON",
    "LOW", "RTX", "BKNG", "SPGI", "BLK", "LMT", "SYK", "MDT",
    "TJX", "VRTX", "CB", "SCHW", "DE", "UPS", "C", "AMGN",
]


def finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def fnum(value: Any, default: float = 0.0) -> float:
    return float(value) if finite(value) else float(default)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class Logger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {message}"
        print(line)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


@dataclass
class Thresholds:
    min_market_cap: float
    min_avg_volume: float
    min_avg_dollar_volume: float
    max_spread_bps: float
    max_annual_vol: float
    max_atr_pct: float
    max_abs_daily_move: float
    min_price: float
    max_candidates: int
    min_score: float


def parse_symbols(raw: str | None, file_path: str | None) -> list[str]:
    symbols: list[str] = []
    if file_path:
        with open(file_path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    symbols.extend(line.replace(",", " ").split())
    if raw:
        symbols.extend(raw.replace(",", " ").split())
    if not symbols:
        symbols = list(DEFAULT_UNIVERSE)
    out: list[str] = []
    seen: set[str] = set()
    for sym in symbols:
        clean = sym.strip().upper()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def parse_json_response(resp_or_text: Any) -> Any:
    if hasattr(resp_or_text, "json"):
        return resp_or_text.json()
    return json.loads(getattr(resp_or_text, "text", resp_or_text))


def new_schwab_client(tokens_file: str):
    try:
        import schwabdev
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'schwabdev'. Run: pip install -r requirements.txt") from exc

    app_key = os.getenv("app_key")
    app_secret = os.getenv("app_secret")
    if not app_key or not app_secret:
        raise RuntimeError("Set env vars app_key/app_secret before running stock_engine.py")
    client = schwabdev.Client(app_key, app_secret, CALLBACK_URL, tokens_file)
    try:
        client.update_tokens()
    except Exception:
        # Some schwabdev versions refresh during construction or quote calls.
        pass
    return client


def fetch_quote_http(client: Any, symbols: list[str]) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'requests'. Run: pip install -r requirements.txt") from exc

    url = "https://api.schwabapi.com/marketdata/v1/quotes"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {client.access_token}"}
    params = {"symbols": ",".join(symbols), "fields": "quote,fundamental,reference"}
    resp = requests.get(url, params=params, headers=headers, timeout=(10, 30))
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Schwab quote auth failed with HTTP {resp.status_code}")
    resp.raise_for_status()
    return parse_json_response(resp) or {}


def fetch_quotes(client: Any, symbols: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i + 50]
        try:
            out.update(fetch_quote_http(client, batch))
        except Exception:
            for sym in batch:
                try:
                    out.update(parse_json_response(client.quote(sym)) or {})
                except Exception:
                    out[sym] = {"error": "quote unavailable"}
    return out


def fetch_history(client: Any, symbol: str, years: int = 2) -> list[dict[str, float]]:
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
            raise RuntimeError("Missing dependency 'requests'. Run: pip install -r requirements.txt") from exc

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
    norm: list[dict[str, float]] = []
    for c in candles:
        if not isinstance(c, dict):
            continue
        try:
            ts = c.get("datetime")
            day = None
            if ts is not None:
                day = datetime.fromtimestamp(float(ts) / 1000, tz=timezone.utc).date().isoformat()
            norm.append({
                "date": day or "",
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume") or 0.0),
            })
        except Exception:
            continue
    return norm


def latest(node: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in node and node.get(key) is not None:
            return node.get(key)
    return default


def quote_parts(raw: dict[str, Any], symbol: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    node = raw.get(symbol) or raw.get(symbol.upper()) or {}
    return (
        node.get("quote") if isinstance(node.get("quote"), dict) else {},
        node.get("fundamental") if isinstance(node.get("fundamental"), dict) else {},
        node.get("reference") if isinstance(node.get("reference"), dict) else {},
    )


def normalize_market_cap(value: Any, last_price: float, shares: Any) -> float:
    cap = fnum(value, 0.0)
    if cap > 0:
        # Schwab/TDA-style fundamentals may report marketCap in millions.
        return cap * 1_000_000.0 if cap < 10_000_000 else cap
    sh = fnum(shares, 0.0)
    if sh > 0 and last_price > 0:
        return sh * last_price
    return 0.0


def moving_average(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def compute_metrics(history: list[dict[str, float]], quote: dict[str, Any], fundamental: dict[str, Any]) -> dict[str, float]:
    closes = [c["close"] for c in history if c.get("close", 0) > 0]
    volumes = [c.get("volume", 0.0) for c in history if c.get("volume", 0) >= 0]
    last = fnum(latest(quote, ("lastPrice", "mark", "regularMarketLastPrice")), closes[-1] if closes else 0.0)
    bid = fnum(latest(quote, ("bidPrice", "regularMarketBidPrice")), 0.0)
    ask = fnum(latest(quote, ("askPrice", "regularMarketAskPrice")), 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 and ask >= bid else last
    spread_bps = ((ask - bid) / mid * 10_000.0) if bid > 0 and ask > 0 and mid > 0 and ask >= bid else 9999.0

    avg_volume = sum(volumes[-60:]) / min(len(volumes), 60) if volumes else 0.0
    avg_dollar_volume = avg_volume * last

    returns: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0:
            returns.append((cur / prev) - 1.0)
    tail_returns = returns[-60:]
    if len(tail_returns) > 1:
        mean_ret = sum(tail_returns) / len(tail_returns)
        variance = sum((r - mean_ret) ** 2 for r in tail_returns) / (len(tail_returns) - 1)
        annual_vol = math.sqrt(variance) * math.sqrt(252)
        max_abs_move = max(abs(r) for r in tail_returns)
    else:
        annual_vol = 9.99
        max_abs_move = 9.99

    trs: list[float] = []
    prev_close = closes[0] if closes else 0.0
    for c in history[1:]:
        hi = c.get("high", 0.0)
        lo = c.get("low", 0.0)
        close = c.get("close", 0.0)
        if hi > 0 and lo > 0 and prev_close > 0:
            trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        prev_close = close
    atr = sum(trs[-14:]) / min(len(trs), 14) if trs else 0.0
    atr_pct = atr / last if last > 0 else 9.99

    eps = fnum(latest(fundamental, ("epsTTM", "eps", "earningsPerShare")), 0.0)
    pe = fnum(latest(fundamental, ("peRatio", "pe", "pE")), 0.0)
    market_cap = normalize_market_cap(
        latest(fundamental, ("marketCap", "marketCapitalization")),
        last,
        latest(fundamental, ("sharesOutstanding", "shares")),
    )

    sma50 = moving_average(closes, 50) or 0.0
    sma200 = moving_average(closes, 200) or 0.0

    return {
        "last": last,
        "bid": bid,
        "ask": ask,
        "spread_bps": spread_bps,
        "avg_volume": avg_volume,
        "avg_dollar_volume": avg_dollar_volume,
        "annual_vol": annual_vol,
        "max_abs_daily_move": max_abs_move,
        "atr": atr,
        "atr_pct": atr_pct,
        "eps": eps,
        "pe": pe,
        "market_cap": market_cap,
        "sma50": sma50,
        "sma200": sma200,
    }


def exclusion_reasons(metrics: dict[str, float], t: Thresholds) -> list[str]:
    reasons: list[str] = []
    if metrics["last"] < t.min_price:
        reasons.append(f"price {metrics['last']:.2f} below minimum {t.min_price:.2f}")
    if metrics["market_cap"] < t.min_market_cap:
        reasons.append(f"market cap {metrics['market_cap']:.0f} below minimum {t.min_market_cap:.0f}")
    if metrics["avg_volume"] < t.min_avg_volume:
        reasons.append(f"avg volume {metrics['avg_volume']:.0f} below minimum {t.min_avg_volume:.0f}")
    if metrics["avg_dollar_volume"] < t.min_avg_dollar_volume:
        reasons.append(f"avg dollar volume {metrics['avg_dollar_volume']:.0f} below minimum {t.min_avg_dollar_volume:.0f}")
    if metrics["spread_bps"] > t.max_spread_bps:
        reasons.append(f"spread {metrics['spread_bps']:.1f} bps above maximum {t.max_spread_bps:.1f}")
    if metrics["annual_vol"] > t.max_annual_vol:
        reasons.append(f"annual volatility {metrics['annual_vol']:.1%} above maximum {t.max_annual_vol:.1%}")
    if metrics["atr_pct"] > t.max_atr_pct:
        reasons.append(f"ATR {metrics['atr_pct']:.1%} of price above maximum {t.max_atr_pct:.1%}")
    if metrics["max_abs_daily_move"] > t.max_abs_daily_move:
        reasons.append(f"max daily move {metrics['max_abs_daily_move']:.1%} above maximum {t.max_abs_daily_move:.1%}")
    if metrics["eps"] <= 0 and metrics["pe"] <= 0:
        reasons.append("profitability unavailable or non-positive EPS/PE")
    return reasons


def score(metrics: dict[str, float], t: Thresholds) -> float:
    market_score = clamp(metrics["market_cap"] / (t.min_market_cap * 5.0), 0, 1) * 18
    liquidity_score = clamp(metrics["avg_dollar_volume"] / (t.min_avg_dollar_volume * 6.0), 0, 1) * 20
    spread_score = clamp(1.0 - (metrics["spread_bps"] / max(t.max_spread_bps, 0.1)), 0, 1) * 17
    vol_score = clamp(1.0 - (metrics["annual_vol"] / max(t.max_annual_vol, 0.01)), 0, 1) * 17
    atr_score = clamp(1.0 - (metrics["atr_pct"] / max(t.max_atr_pct, 0.001)), 0, 1) * 10
    profit_score = (10 if metrics["eps"] > 0 else 6 if metrics["pe"] > 0 else 0)
    trend_score = 0.0
    if metrics["sma200"] > 0:
        trend_score += 5 if metrics["last"] >= metrics["sma200"] else 2
    if metrics["sma50"] > 0:
        trend_score += 3 if metrics["last"] >= metrics["sma50"] else 1
    return round(market_score + liquidity_score + spread_score + vol_score + atr_score + profit_score + trend_score, 2)


def suggestions(metrics: dict[str, float]) -> tuple[float, list[dict[str, float]], float]:
    vol = metrics["annual_vol"]
    atr_pct = metrics["atr_pct"]
    if vol <= 0.30 and atr_pct <= 0.025:
        cap = 0.08
        stages = [{"thr": 3, "frac": 0.25}, {"thr": 6, "frac": 0.35}, {"thr": 9, "frac": 0.40}]
        atr_k = 1.1
    elif vol <= 0.45 and atr_pct <= 0.035:
        cap = 0.06
        stages = [{"thr": 4, "frac": 0.25}, {"thr": 7, "frac": 0.35}, {"thr": 11, "frac": 0.40}]
        atr_k = 1.3
    else:
        cap = 0.04
        stages = [{"thr": 5, "frac": 0.20}, {"thr": 9, "frac": 0.35}, {"thr": 14, "frac": 0.45}]
        atr_k = 1.6
    return cap, stages, atr_k


def evaluate_symbol(
    symbol: str,
    quote_map: dict[str, Any],
    history: list[dict[str, float]],
    thresholds: Thresholds,
) -> tuple[dict[str, Any] | None, list[str], dict[str, float]]:
    quote, fundamental, _reference = quote_parts(quote_map, symbol)
    metrics = compute_metrics(history, quote, fundamental)
    rejects = exclusion_reasons(metrics, thresholds)
    if rejects:
        return None, rejects, metrics
    points = score(metrics, thresholds)
    if points < thresholds.min_score:
        return None, [f"score {points:.2f} below minimum {thresholds.min_score:.2f}"], metrics
    cap, stages, atr_k = suggestions(metrics)
    reason = (
        f"large-cap profitable stock; market_cap=${metrics['market_cap'] / 1e9:.1f}B; "
        f"avg_dollar_volume=${metrics['avg_dollar_volume'] / 1e6:.0f}M; "
        f"spread={metrics['spread_bps']:.1f}bps; vol={metrics['annual_vol']:.1%}; "
        f"ATR={metrics['atr_pct']:.1%}"
    )
    risks = []
    if metrics["annual_vol"] > 0.40:
        risks.append("higher realized volatility; use smaller cap and wider stages")
    if metrics["last"] < metrics["sma200"] and metrics["sma200"] > 0:
        risks.append("below 200-day average; trend risk")
    if metrics["spread_bps"] > thresholds.max_spread_bps * 0.60:
        risks.append("spread is acceptable but not especially tight")
    if not risks:
        risks.append("single-stock idiosyncratic risk remains higher than broad ETFs")
    return {
        "symbol": symbol,
        "score": points,
        "reason": reason,
        "risks": risks,
        "suggested_cap": cap,
        "suggested_buy_stages": stages,
        "suggested_atr_k": atr_k,
    }, [], metrics


def filter_history_as_of(history: list[dict[str, float]], as_of: str | None) -> list[dict[str, float]]:
    if not as_of:
        return history
    try:
        cutoff = date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError("--as-of must be YYYY-MM-DD") from exc
    filtered = []
    for candle in history:
        cdate = candle.get("date")
        if not cdate:
            filtered.append(candle)
            continue
        try:
            if date.fromisoformat(str(cdate)) <= cutoff:
                filtered.append(candle)
        except ValueError:
            continue
    return filtered


def write_json(path: str, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)


def write_approved(path: str, symbols: list[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for sym in symbols:
            fh.write(sym + "\n")
    os.replace(tmp, p)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Standalone large-cap stock selection engine. Does not trade.")
    ap.add_argument("--symbols", help="Comma or space separated symbols. Defaults to a conservative large-cap universe.")
    ap.add_argument("--symbols-file", help="Optional newline/comma separated universe file.")
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--approved-output", default=DEFAULT_APPROVED)
    ap.add_argument("--log-file", default=DEFAULT_LOG)
    ap.add_argument("--tokens-file", default=DEFAULT_TOKENS_FILE)
    ap.add_argument("--dry-run", action="store_true", help="Evaluate and log without writing approved_symbols.txt.")
    ap.add_argument("--backtest", action="store_true", help="Evaluate using history through --as-of for price/volume metrics.")
    ap.add_argument("--as-of", help="Backtest cutoff date, YYYY-MM-DD.")
    ap.add_argument("--max-candidates", type=int, default=12)
    ap.add_argument("--min-score", type=float, default=55.0)
    ap.add_argument("--min-market-cap", type=float, default=50_000_000_000.0)
    ap.add_argument("--min-avg-volume", type=float, default=1_000_000.0)
    ap.add_argument("--min-avg-dollar-volume", type=float, default=100_000_000.0)
    ap.add_argument("--max-spread-bps", type=float, default=12.0)
    ap.add_argument("--max-annual-vol", type=float, default=0.65)
    ap.add_argument("--max-atr-pct", type=float, default=0.055)
    ap.add_argument("--max-abs-daily-move", type=float, default=0.12)
    ap.add_argument("--min-price", type=float, default=10.0)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    logger = Logger(args.log_file)
    thresholds = Thresholds(
        min_market_cap=args.min_market_cap,
        min_avg_volume=args.min_avg_volume,
        min_avg_dollar_volume=args.min_avg_dollar_volume,
        max_spread_bps=args.max_spread_bps,
        max_annual_vol=args.max_annual_vol,
        max_atr_pct=args.max_atr_pct,
        max_abs_daily_move=args.max_abs_daily_move,
        min_price=args.min_price,
        max_candidates=max(1, args.max_candidates),
        min_score=args.min_score,
    )

    if args.as_of and not args.backtest:
        logger.log("[WARN] --as-of supplied without --backtest; enabling backtest mode.")
        args.backtest = True

    symbols = parse_symbols(args.symbols, args.symbols_file)
    logger.log(f"[START] mode={'backtest' if args.backtest else 'live'} dry_run={args.dry_run} symbols={len(symbols)}")
    logger.log("[SAFETY] This engine writes advisory candidate files only; it does not submit orders.")
    if args.backtest:
        logger.log(f"[BACKTEST] price/volume metrics use history through {args.as_of or 'latest available'}; fundamentals are current broker fields.")

    client = new_schwab_client(args.tokens_file)
    quote_map = fetch_quotes(client, symbols)

    candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    metrics_by_symbol: dict[str, dict[str, float]] = {}

    for symbol in symbols:
        try:
            history = fetch_history(client, symbol)
            if args.backtest:
                history = filter_history_as_of(history, args.as_of)
            if len(history) < 80:
                reason = f"insufficient history ({len(history)} daily bars)"
                logger.log(f"[EXCLUDE] {symbol}: {reason}")
                exclusions.append({"symbol": symbol, "reasons": [reason]})
                continue
            candidate, rejects, metrics = evaluate_symbol(symbol, quote_map, history, thresholds)
            metrics_by_symbol[symbol] = {k: round(v, 6) for k, v in metrics.items()}
            if candidate:
                logger.log(f"[INCLUDE] {symbol}: score={candidate['score']:.2f}; {candidate['reason']}")
                candidates.append(candidate)
            else:
                logger.log(f"[EXCLUDE] {symbol}: {'; '.join(rejects)}")
                exclusions.append({"symbol": symbol, "reasons": rejects})
        except Exception as exc:
            logger.log(f"[EXCLUDE] {symbol}: evaluation error: {exc}")
            exclusions.append({"symbol": symbol, "reasons": [f"evaluation error: {exc}"]})

    candidates.sort(key=lambda c: (-float(c["score"]), c["symbol"]))
    selected = candidates[:thresholds.max_candidates]
    approved_symbols = [c["symbol"] for c in selected]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "backtest" if args.backtest else "live",
        "dry_run": bool(args.dry_run),
        "universe_size": len(symbols),
        "thresholds": thresholds.__dict__,
        "approved_symbols_file": args.approved_output,
        "candidates": selected,
        "excluded_count": len(exclusions),
        "exclusions": exclusions,
        "metrics": metrics_by_symbol,
        "notes": [
            "No automatic trading is enabled by this engine.",
            "BuyLow should consume only approved_symbols.txt if expanded symbols are wired in later.",
        ],
    }

    write_json(args.output, payload)
    logger.log(f"[WRITE] candidates={len(selected)} output={args.output}")
    if args.dry_run:
        logger.log(f"[DRY-RUN] skipped approved symbol write: {args.approved_output}")
    else:
        write_approved(args.approved_output, approved_symbols)
        logger.log(f"[WRITE] approved_symbols={len(approved_symbols)} output={args.approved_output}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
