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
LOG_DIR = Path(os.getenv("TREND_LOG_DIR", r"C:\temp\logs_trend"))
DEFAULT_DATA_DIR = Path(os.getenv("TREND_DATA_DIR", r"C:\temp"))

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
    score: float
    proposal_price: float
    proposal_pct: float
    proposal_dollars: float
    invalidation_level: float
    data_as_of: str
    reasons: list[str]
    rejections: list[str]
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


def score_candidate(
    symbol: str,
    bars: list[Bar],
    config: dict[str, Any],
    state: dict[str, Any],
    selected_sectors: dict[str, int],
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
    metrics: dict[str, Any] = {}

    if len(bars) < min_bars:
        return Candidate(symbol, sector, "rejected", 0.0, 0.0, proposal_pct, account_value * proposal_pct, 0.0, "", [], ["insufficient price history"], {"bars": len(bars)})

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
    elif latest.close > sma20:
        score += 14
        reasons.append("price above SMA20")
    else:
        score -= 30
        rejections.append("price below SMA20")

    if sma20 is not None and sma50 is not None and sma20 > sma50:
        score += 18
        reasons.append("SMA20 above SMA50")
    else:
        score -= 28
        rejections.append("weak trend quality")

    if dist_high < 0.01:
        score -= 8
        rejections.append("overextended")
    elif 0.02 <= dist_high <= 0.08:
        score += 16
        reasons.append("moderate pullback")
    elif dist_high <= 0.15:
        score += 6
        reasons.append("constructive pullback")
    else:
        score -= 18
        rejections.append("too far from 52-week high")

    min_avg_dollar_volume = fnum(config.get("min_avg_dollar_volume"), 1000000000.0)
    if avg_dollar_volume >= min_avg_dollar_volume:
        score += 10
        reasons.append("liquid large-cap")
    else:
        score -= 20
        rejections.append("insufficient liquidity")

    max_spread_bps = fnum(config.get("max_spread_bps"), 15.0)
    if spread_bps <= max_spread_bps:
        score += 8
        reasons.append("tight spread")
    else:
        score -= 15
        rejections.append("poor spread quality")

    max_atr_pct = fnum(config.get("max_atr_pct"), 0.045)
    if atr_pct > max_atr_pct:
        score -= 18
        rejections.append("volatility penalty")
    elif atr_pct > max_atr_pct * 0.70:
        score -= 6
        reasons.append("moderate volatility")

    if recent_vol > fnum(config.get("max_recent_abs_daily_move"), 0.035):
        score -= 8
        rejections.append("volatile recent tape")

    if selected_sectors.get(sector, 0) >= sector_cap:
        score -= 20
        rejections.append("sector already represented")

    if in_cooldown:
        score -= 35
        rejections.append("cooldown active")

    if recent_buy:
        score -= 18
        rejections.append("recent participation penalty")

    if data_age > max_data_age_days:
        score -= 25
        rejections.append("stale market data")

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
    status = "proposal" if score >= min_score and not rejections else "rejected"
    if status == "proposal":
        reasons = summarize_reasons(reasons)

    return Candidate(
        symbol=symbol,
        sector=sector,
        status=status,
        score=score,
        proposal_price=round(latest.close, 2),
        proposal_pct=proposal_pct,
        proposal_dollars=round(account_value * proposal_pct, 2),
        invalidation_level=round(invalidation, 2),
        data_as_of=latest.day.isoformat(),
        reasons=reasons,
        rejections=dedupe(rejections),
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
        f"Score {candidate.score:.1f}. {reason_text}."
    )


def format_ranking(candidate: Candidate) -> str:
    m = candidate.metrics
    return (
        f"{candidate.symbol}: score {candidate.score:.1f}, status={candidate.status}, "
        f"last={m.get('last')}, SMA20={m.get('sma20')}, SMA50={m.get('sma50')}, "
        f"from_high={m.get('distance_from_52w_high_pct')}%, ATR={m.get('atr_pct')}%, "
        f"sector={candidate.sector}, data={candidate.data_as_of}"
    )


def format_rejection(candidate: Candidate) -> str:
    reason = "; ".join(candidate.rejections) if candidate.rejections else "below shortlist threshold"
    return f"{candidate.symbol}: {reason}. Score {candidate.score:.1f}."


def build_report(config: dict[str, Any], state: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    watchlist = [str(s).upper() for s in config.get("watchlist", [])]
    max_proposals = int(fnum(config.get("max_proposals"), 3))
    selected_sectors: dict[str, int] = {}
    candidates: list[Candidate] = []

    for symbol in watchlist:
        bars = load_price_csv(symbol, data_dir)
        candidate = score_candidate(symbol, bars, config, state, selected_sectors)
        candidates.append(candidate)
        if candidate.status == "proposal":
            selected_sectors[candidate.sector] = selected_sectors.get(candidate.sector, 0) + 1

    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    shortlist = [c for c in ranked if c.status == "proposal"][:max_proposals]
    shortlist_symbols = {c.symbol for c in shortlist}
    rejected = [c for c in ranked if c.symbol not in shortlist_symbols]

    return {
        "strategy": "trend_rider",
        "mode": MODE,
        "manual_actions_only": MANUAL_ACTIONS_ONLY,
        "live_orders_enabled": LIVE_ORDERS_ENABLED,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "log_dir": str(LOG_DIR),
        "shortlist": [asdict(c) for c in shortlist],
        "rankings": [asdict(c) for c in ranked],
        "rejected": [asdict(c) for c in rejected],
        "warnings": [
            "Proposal-only output. No live orders are placed.",
            "Local CSV data may be stale unless refreshed externally.",
        ],
    }


def render_report(report: dict[str, Any], logger: TeeLogger) -> None:
    logger.write("BUY TODAY SHORTLIST")
    shortlist = [Candidate(**c) for c in report.get("shortlist", [])]
    if not shortlist:
        logger.write("No proposal-qualified trend entries today.")
    for candidate in shortlist:
        logger.write(format_proposal(candidate))

    logger.write("")
    logger.write("WATCHLIST RANKINGS")
    rankings = [Candidate(**c) for c in report.get("rankings", [])]
    for candidate in rankings:
        logger.write(format_ranking(candidate))

    logger.write("")
    logger.write("REJECTED CANDIDATES")
    rejected = [Candidate(**c) for c in report.get("rejected", [])]
    if not rejected:
        logger.write("None.")
    for candidate in rejected:
        logger.write(format_rejection(candidate))


def update_state(state: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    state = dict(state)
    state["mode"] = MODE
    state["manual_actions_only"] = MANUAL_ACTIONS_ONLY
    state["live_orders_enabled"] = LIVE_ORDERS_ENABLED
    state["last_evaluation"] = report.get("generated_at")
    state.setdefault("last_proposed_symbols", {})
    state.setdefault("last_accepted_symbols", {})
    today = today_iso()
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
    parser.add_argument("--no-write-state", action="store_true", help="Do not update trend_state.json")
    parser.add_argument("--json", action="store_true", help="Print JSON report after text output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    state_path = Path(args.state)
    cache_path = Path(args.cache)
    data_dir = Path(args.data_dir)

    config = load_json(config_path)
    state = load_json(state_path, {"mode": MODE, "last_proposed_symbols": {}, "last_accepted_symbols": {}})
    logger = TeeLogger(LOG_DIR)

    report = build_report(config, state, data_dir)
    render_report(report, logger)
    save_json(cache_path, report)
    if not args.no_write_state:
        save_json(state_path, update_state(state, report))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
