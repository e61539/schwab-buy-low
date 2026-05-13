from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import subprocess
import os
import re
import ast
import json
import sys
import time
import threading
from datetime import datetime, time as dtime
from io import BytesIO

import requests
import urllib3
from pypdf import PdfReader
import urllib.request
import urllib.error
from pathlib import Path
from fastapi import Header, HTTPException

app = FastAPI()

# Resolve dashboard runtime and repo-local paths independently of cwd.
DASHBOARD_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECT_ROOT = Path(r"C:\Users\cheng_hamn078\scripts\schwab-buy-low")
ROOT = Path(os.getenv("BUYLOW_HOME", str(DEFAULT_PROJECT_ROOT))).resolve()
CONFIG_DIR = ROOT / "config"
RUNTIME_DIR = ROOT / "runtime"
CACHE_DIR = RUNTIME_DIR / "cache"
STATE_DIR = RUNTIME_DIR / "state"
for _path in (ROOT, DASHBOARD_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

print(f"[DASHBOARD] running from {DASHBOARD_DIR}")
print(f"[DASHBOARD] project root = {ROOT}")

TRADE_SERVER_BASE = os.getenv("TRADE_SERVER_BASE", "http://127.0.0.1:8080").rstrip("/")
DEFAULT_CAPITAL_READINESS_FILE = (
    r"C:\temp\capital_readiness.json"
    if os.name == "nt"
    else "/tmp/capital_readiness.json"
)
CAPITAL_READINESS_FILE = os.getenv("BUYLOW_CAPITAL_READINESS_FILE", DEFAULT_CAPITAL_READINESS_FILE)
DEFAULT_CAPITAL_UTILIZATION_FILE = (
    r"C:\temp\capital_utilization.json"
    if os.name == "nt"
    else "/tmp/capital_utilization.json"
)
CAPITAL_UTILIZATION_FILE = os.getenv("BUYLOW_CAPITAL_UTILIZATION_FILE", DEFAULT_CAPITAL_UTILIZATION_FILE)
TREND_RIDER_DIR = ROOT / "strategies" / "trend_rider"
TREND_RIDER_CONFIG_FILE = Path(os.getenv("TREND_RIDER_CONFIG_FILE", str(TREND_RIDER_DIR / "trend_config.json")))
TREND_RIDER_CACHE_FILE = Path(os.getenv("TREND_RIDER_CACHE_FILE", str(TREND_RIDER_DIR / "trend_cache.json")))
TREND_RIDER_ETF_UNIVERSE = os.getenv("TREND_RIDER_ETF_UNIVERSE", "")

BUYLOW_LOG_CACHE_TTL_SEC = float(os.getenv("BUYLOW_LOG_CACHE_TTL_SEC", "25"))
BUYLOW_LOG_TIMEOUT_SEC = float(os.getenv("BUYLOW_LOG_TIMEOUT_SEC", "8"))
_buylow_log_cache = {"ts": 0.0, "data": None}
_buylow_log_cache_lock = threading.Lock()

POSITIONS_PROXY_TIMEOUT_SEC = float(os.getenv("POSITIONS_PROXY_TIMEOUT_SEC", "30"))
POSITIONS_PROXY_RETRIES = int(os.getenv("POSITIONS_PROXY_RETRIES", "2"))
POSITIONS_PROXY_RETRY_SLEEP_SEC = float(os.getenv("POSITIONS_PROXY_RETRY_SLEEP_SEC", "0.75"))
POSITIONS_PROXY_INFLIGHT_WAIT_SEC = float(os.getenv("POSITIONS_PROXY_INFLIGHT_WAIT_SEC", "1.0"))
_positions_fetch_lock = threading.Lock()
_positions_cache_lock = threading.Lock()
_positions_lkg_cache: dict[str, dict] = {}

# ====== CONFIG ======
# Put a long random string here.
API_KEY = os.environ.get("TRADE_API_KEY")

# Your interpreter (optional). If your scripts require python313, set it explicitly.
PYTHON_EXE = os.environ.get("DASH_PYTHON_EXE", "python")

# Path to your existing quote script that prints JSON (or plain text) to stdout.
QUOTE_SCRIPT = os.environ.get(
    "DASH_QUOTE_SCRIPT",
    r"C:\Users\cheng\PycharmProjects\quote\quote.py"
)

POSITION_SCRIPT = os.environ.get(
    "DASH_POSITION_SCRIPT",
    r"C:\Users\cheng_hamn078\source\repos\schwab_position\schwab_position\schwab_position.py"
)

# ====== SECURITY ======
def require_key(request: Request):
    key = request.query_params.get("k") or request.headers.get("x-api-key")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ====== HELPERS ======
def run_quote(symbol: str) -> str:
    # Call your existing python script: python quote.py SPY
    result = subprocess.run(
        [PYTHON_EXE, QUOTE_SCRIPT, symbol],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        return f"ERROR:\n{result.stderr}"
    return result.stdout.strip()

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def run_position_one(symbol: str) -> dict:
    sym = symbol.strip().upper()

    r = subprocess.run(
        [PYTHON_EXE, POSITION_SCRIPT, "--combine", "--symbol", sym, "--json"],
        capture_output=True,
        text=True,
        timeout=90
    )

    if r.returncode != 0:
        raise ValueError(f"positions script failed:\n{r.stderr}")

    raw = r.stdout

    # Find first real JSON block (line starting with {)
    lines = raw.splitlines()
    json_start_index = None

    for i, line in enumerate(lines):
        if line.strip().startswith("{"):
            json_start_index = i
            break

    if json_start_index is None:
        raise ValueError("JSON block not found in output")

    json_text = "\n".join(lines[json_start_index:])

    data = json.loads(json_text)

    if sym not in data:
        raise ValueError(f"Symbol {sym} not found in JSON output")

    return data[sym]

# ====== CAPITAL READINESS HELPERS ======
def empty_capital_readiness() -> dict:
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

def read_json_file(path: str) -> dict:
    try:
        p = os.path.abspath(path)
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def normalize_symbol(value) -> str:
    return str(value or "").strip().upper()

def split_symbols(value: str) -> list[str]:
    return [sym for sym in (normalize_symbol(part) for part in re.split(r"[,\s]+", value or "")) if sym]

TREND_ACTION_PRIORITY = {
    "BUY_CANDIDATE": 0,
    "ADD_ON_CANDIDATE": 1,
    "HOLD_STRONG": 2,
    "HOLD_DEFENSIVE": 3,
    "WAIT_FOR_COOLDOWN": 4,
    "NEUTRAL": 5,
    "HOLD_WEAKENING": 6,
    "AVOID_FOR_NOW": 7,
}

TREND_BADGE_CLASS = {
    "BUY_CANDIDATE": "buy",
    "ADD_ON_CANDIDATE": "buy",
    "HOLD_STRONG": "strong",
    "HOLD_DEFENSIVE": "defensive",
    "WAIT_FOR_COOLDOWN": "cooldown",
    "NEUTRAL": "neutral",
    "HOLD_WEAKENING": "weak",
    "AVOID_FOR_NOW": "avoid",
}

TREND_ACTION_EXPLANATION = {
    "BUY_CANDIDATE": "Strong uptrend near highs with constructive pullback.",
    "ADD_ON_CANDIDATE": "Existing trend position may support a measured add-on.",
    "HOLD_STRONG": "Active trend position with healthy momentum.",
    "HOLD_DEFENSIVE": "Defensive holding with weaker momentum characteristics.",
    "HOLD_WEAKENING": "Trend weakening below SMA20; monitor for recovery or breakdown.",
    "WAIT_FOR_COOLDOWN": "Recent signal or entry is cooling down to avoid repeated chasing.",
    "AVOID_FOR_NOW": "Trend setup is not currently suitable for entry.",
    "NEUTRAL": "No active buy signal; keep monitoring.",
}

TREND_ACTION_GUIDANCE = {
    "BUY_CANDIDATE": "Eligible for new entry.",
    "ADD_ON_CANDIDATE": "Eligible for add-on review.",
    "HOLD_STRONG": "Continue holding trend position.",
    "HOLD_DEFENSIVE": "Continue monitoring defensive allocation.",
    "HOLD_WEAKENING": "Monitor SMA20 recovery and invalidation level.",
    "WAIT_FOR_COOLDOWN": "Re-evaluate after cooldown expiry.",
    "AVOID_FOR_NOW": "Do not enter unless trend quality improves.",
    "NEUTRAL": "Watch only.",
}

TREND_STATUS_COLOR = {
    "BUY_CANDIDATE": "green",
    "ADD_ON_CANDIDATE": "green",
    "HOLD_STRONG": "green",
    "HOLD_DEFENSIVE": "yellow",
    "HOLD_WEAKENING": "orange",
    "WAIT_FOR_COOLDOWN": "yellow",
    "AVOID_FOR_NOW": "red",
    "NEUTRAL": "gray",
}

TREND_REASON_DESCRIPTIONS = {
    "accepted_signal": "Signal was previously accepted and is pending or cooling down.",
    "accepted_signal_pending_entry": "Accepted signal is pending entry confirmation.",
    "active_trend_position_holding": "Symbol is tracked as an active Trend Rider holding.",
    "constructive_pullback": "Price has pulled back modestly while the uptrend remains intact.",
    "cooldown_active": "Cooldown is active to prevent repeated entries.",
    "extended_distance_from_high": "Price is materially below recent leadership levels.",
    "healthy_pullback_from_high": "Price has pulled back modestly while the uptrend remains intact.",
    "liquid_large_cap": "Symbol has large-cap liquidity characteristics.",
    "moderate_distance_from_high": "Price has pulled back from highs but trend may still be constructive.",
    "moderate_pullback": "Price has pulled back from highs but trend may still be constructive.",
    "moderate_volatility": "Volatility is within the strategy's acceptable range.",
    "near_52w_high": "Price remains near recent leadership levels.",
    "no_broker_qty": "No broker position is currently detected.",
    "overextended": "Price may be stretched above its trend baseline.",
    "owned": "Symbol is already held in the account.",
    "price_above_sma20": "Price is above the 20-day moving average.",
    "price_below_sma20": "Price is below the 20-day moving average.",
    "recent_participation_penalty": "Recent participation reduces urgency for another entry.",
    "recent_shortlist_signal": "Symbol appeared recently on the shortlist.",
    "sector_already_represented": "The sector already has Trend Rider representation.",
    "sma20_above_sma50": "20-day moving average is above the 50-day moving average.",
    "tight_spread": "Bid/ask spread is tight, suggesting good liquidity.",
    "too_far_from_52w_high": "Price is materially below recent leadership levels.",
    "weak_trend_quality": "Trend quality is weaker or less persistent.",
}

def trend_action_priority(action: str) -> int:
    return TREND_ACTION_PRIORITY.get(normalize_symbol(action), 99)

def trend_badge_class(action: str) -> str:
    return TREND_BADGE_CLASS.get(normalize_symbol(action), "neutral")

def build_watchlist_explanation(item: dict) -> str:
    action = normalize_symbol(item.get("action_hint")) or "NEUTRAL"
    return TREND_ACTION_EXPLANATION.get(action, TREND_ACTION_EXPLANATION["NEUTRAL"])

def build_action_guidance(action: str) -> str:
    return TREND_ACTION_GUIDANCE.get(normalize_symbol(action), TREND_ACTION_GUIDANCE["NEUTRAL"])

def trend_status_color(action: str) -> str:
    return TREND_STATUS_COLOR.get(normalize_symbol(action), "gray")

def describe_reason_code(code: str) -> str:
    normalized = normalize_symbol(code).lower()
    if normalized in TREND_REASON_DESCRIPTIONS:
        return TREND_REASON_DESCRIPTIONS[normalized]
    return normalized.replace("_", " ").capitalize() + "."

def describe_reasons(reason_codes: list) -> dict[str, str]:
    return {str(code): describe_reason_code(str(code)) for code in reason_codes}

def format_cooldown_remaining(minutes) -> str:
    try:
        remaining = max(0, int(float(minutes)))
    except Exception:
        return ""
    hours, mins = divmod(remaining, 60)
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"

def build_cooldown_summary(item: dict) -> dict | None:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    reason = str(metrics.get("cooldown_reason") or "").strip()
    effect = str(metrics.get("cooldown_effect") or "").strip()
    blocks = str(metrics.get("cooldown_blocks") or "").strip()
    remaining = metrics.get("cooldown_remaining_minutes")
    try:
        remaining_minutes = int(float(remaining)) if remaining is not None else 0
    except Exception:
        remaining_minutes = 0

    is_active = bool(reason) and effect != "informational_only" and remaining_minutes > 0 and (
        bool(blocks) or effect.startswith("blocks_") or "blocks" in effect
    )
    if not is_active:
        return None

    return {
        "active": True,
        "reason": reason,
        "effect": effect,
        "expires": metrics.get("cooldown_expires") or metrics.get("post_entry_cooldown_expires") or "",
        "remaining": format_cooldown_remaining(remaining_minutes),
        "source": metrics.get("cooldown_config_source") or metrics.get("post_entry_cooldown_config_source") or "",
        "duration_days": metrics.get("cooldown_duration_days"),
        "summary": f"Cooldown active after {reason.replace('_', ' ')}; prevents repeated chasing and re-entry.",
    }

def append_unique_symbol(symbols: list[str], value) -> None:
    sym = normalize_symbol(value)
    if sym and sym not in symbols:
        symbols.append(sym)

def configured_trend_etfs(config: dict) -> list[str]:
    symbols: list[str] = []

    for sym in split_symbols(TREND_RIDER_ETF_UNIVERSE):
        append_unique_symbol(symbols, sym)

    metadata = config.get("symbol_metadata") if isinstance(config.get("symbol_metadata"), dict) else {}
    watchlist = [normalize_symbol(sym) for sym in config.get("watchlist", []) if normalize_symbol(sym)]
    for sym in watchlist:
        item = metadata.get(sym) if isinstance(metadata.get(sym), dict) else {}
        if normalize_symbol(item.get("sector")) == "ETF":
            append_unique_symbol(symbols, sym)

    return symbols

def configured_trend_watchlist(config: dict, report: dict) -> list[str]:
    symbols: list[str] = []

    watchlist = [normalize_symbol(sym) for sym in config.get("watchlist", []) if normalize_symbol(sym)]
    for sym in watchlist:
        append_unique_symbol(symbols, sym)

    for item in trend_report_items(report):
        append_unique_symbol(symbols, item.get("symbol"))

    return symbols

def trend_report_items(report: dict) -> list[dict]:
    items_out: list[dict] = []
    for section in ("rankings", "shortlist", "new_entries", "existing_holdings", "pending_signals", "rejected"):
        items = report.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                items_out.append(item)
    return items_out

def trend_item_by_symbol(report: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in trend_report_items(report):
        sym = normalize_symbol(item.get("symbol"))
        if sym and sym not in out:
            out[sym] = item
    return out

def normalize_trend_dashboard_item(symbol: str, item: dict | None) -> dict:
    item = item or {}
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    action = normalize_symbol(item.get("action_hint")) or "NEUTRAL"
    score = float(item.get("score") or 0.0)
    status = str(item.get("status") or "neutral")
    reason_codes = item.get("reason_codes") if isinstance(item.get("reason_codes"), list) else []
    return {
        "symbol": symbol,
        "score": score,
        "status": status,
        "action": action,
        "action_hint": action,
        "badge_class": trend_badge_class(action),
        "priority": trend_action_priority(action),
        "status_color": trend_status_color(action),
        "explanation": build_watchlist_explanation(item),
        "action_guidance": build_action_guidance(action),
        "last": metrics.get("last"),
        "sma20": metrics.get("sma20"),
        "sma50": metrics.get("sma50"),
        "from_high_pct": metrics.get("from_high_pct", metrics.get("distance_from_52w_high_pct")),
        "cooldown_reason": metrics.get("cooldown_reason"),
        "cooldown_effect": metrics.get("cooldown_effect"),
        "cooldown_summary": build_cooldown_summary(item),
        "reasons": item.get("reasons") if isinstance(item.get("reasons"), list) else [],
        "reason_codes": reason_codes,
        "reason_descriptions": describe_reasons(reason_codes),
        "buy_enabled": action == "BUY_CANDIDATE",
        "visible": True,
        "data_available": bool(item),
    }

def build_trend_action_summary(items: list[dict]) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {action: [] for action in TREND_ACTION_PRIORITY}
    for item in items:
        action = normalize_symbol(item.get("action")) or "NEUTRAL"
        summary.setdefault(action, []).append(normalize_symbol(item.get("symbol")))
    return {action: symbols for action, symbols in summary.items() if symbols}

def load_trend_rider_dashboard() -> dict:
    config = read_json_file(str(TREND_RIDER_CONFIG_FILE))
    report = read_json_file(str(TREND_RIDER_CACHE_FILE))
    watchlist = configured_trend_watchlist(config, report)
    etf_symbols = configured_trend_etfs(config)
    by_symbol = trend_item_by_symbol(report)

    items = [normalize_trend_dashboard_item(sym, by_symbol.get(sym)) for sym in watchlist]
    items.sort(key=lambda item: (item["priority"], -float(item.get("score") or 0.0), item["symbol"]))
    etf_items = [normalize_trend_dashboard_item(sym, by_symbol.get(sym)) for sym in etf_symbols]
    etf_items.sort(key=lambda item: (item["priority"], -float(item.get("score") or 0.0), item["symbol"]))

    raw_rankings = report.get("rankings", [])
    raw_action_summary = report.get("action_summary", {})
    action_summary = build_trend_action_summary(items)
    etf_action_summary = build_trend_action_summary(etf_items)
    return {
        "ok": True,
        "strategy": "trend_rider",
        "generated_at": report.get("generated_at", ""),
        "config_file": str(TREND_RIDER_CONFIG_FILE),
        "cache_file": str(TREND_RIDER_CACHE_FILE),
        "etf_universe": etf_symbols,
        "trend_etf_symbols": etf_symbols,
        "watchlist_symbols": watchlist,
        "symbols": watchlist,
        "items": items,
        "watchlist": items,
        "etfs": etf_items,
        "watchlist_etfs": etf_items,
        "trend_watchlist": items,
        "rankings": items,
        "raw_rankings": raw_rankings,
        "action_summary": action_summary,
        "watchlist_action_summary": action_summary,
        "etf_action_summary": etf_action_summary,
        "raw_action_summary": raw_action_summary,
        "warnings": report.get("warnings", []),
    }

def parse_iso_datetime(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

def is_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)

def is_capital_readiness_stale(generated_at: str, stale_minutes: int = 30) -> bool:
    generated = parse_iso_datetime(generated_at)
    if generated is None:
        return True
    now = datetime.now(generated.tzinfo) if generated.tzinfo else datetime.now()
    if not is_market_hours(now):
        return False
    return (now - generated).total_seconds() > stale_minutes * 60

def load_capital_readiness() -> dict:
    payload = read_json_file(CAPITAL_READINESS_FILE)
    if not payload:
        return empty_capital_readiness()

    out = empty_capital_readiness()
    out.update(payload)
    out["mode"] = "advisory_only"
    out["blocked_symbols"] = out.get("blocked_symbols") if isinstance(out.get("blocked_symbols"), list) else []
    out["manual_action_required"] = bool(out.get("manual_action_required"))
    out["merrill_reserve_configured"] = bool(out.get("merrill_reserve_configured"))
    out["is_stale"] = is_capital_readiness_stale(str(out.get("generated_at") or ""))
    return out

def empty_capital_utilization() -> dict:
    return {
        "as_of": "",
        "source": CAPITAL_UTILIZATION_FILE,
        "source_stale": True,
        "swvxx_cash_reserve": 0,
        "account_total_value": 0,
        "current_invested_value": 0,
        "current_invested_pct": 0,
        "target_deployment_low": 0,
        "target_deployment_high": 0,
        "remaining_to_deploy_low": 0,
        "remaining_to_deploy_high": 0,
        "symbols": [],
        "deployment_schedule": [],
        "warnings": ["Capital utilization file missing or invalid."],
        "manual_actions_only": True,
        "is_stale": True,
    }

def load_capital_utilization() -> dict:
    payload = read_json_file(CAPITAL_UTILIZATION_FILE)
    if not payload:
        return empty_capital_utilization()

    out = empty_capital_utilization()
    out.update(payload)
    out["symbols"] = out.get("symbols") if isinstance(out.get("symbols"), list) else []
    out["deployment_schedule"] = out.get("deployment_schedule") if isinstance(out.get("deployment_schedule"), list) else []
    out["warnings"] = out.get("warnings") if isinstance(out.get("warnings"), list) else []
    out["manual_actions_only"] = True
    out["is_stale"] = is_capital_readiness_stale(str(out.get("as_of") or ""))
    return out
    
# ====== MUTUAL FUND DIVIDEND (7-day yield) HELPERS ======
MERRILL_RATE_SHEET_URL = os.environ.get(
    "MERRILL_RATE_SHEET_URL",
    "https://olui2.fs.ml.com/Publish/Content/application/pdf/GWMOL/ICCRateSheet.pdf"
)

_merrill_cache = {"ts": 0.0, "ttl": 3600, "data": None}  # cache 1 hour

def get_merrill_mmf_yields() -> dict:
    """
    Returns yields like {"POIXX": "3.74%", "TMCXX": "3.76%"} parsed from Merrill ICCRateSheet PDF.
    """
    now = time.time()
    if _merrill_cache["data"] and (now - _merrill_cache["ts"] < _merrill_cache["ttl"]):
        return _merrill_cache["data"]

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    r = requests.get(MERRILL_RATE_SHEET_URL, verify=False, timeout=20)
    r.raise_for_status()

    reader = PdfReader(BytesIO(r.content))

    # Historically the rates are on page 3 (0-indexed 2).
    page_index = 2
    if page_index >= len(reader.pages):
        raise ValueError("Rate sheet PDF format changed (missing expected page).")

    text = reader.pages[page_index].extract_text() or ""
    lines = text.splitlines()

    dic = {}
    for line in lines:
        if "%" not in line:
            continue

        parts = line.split("%")
        if len(parts) <= 1:
            continue

        s = parts[-2]
        if "XX" not in s:
            continue

        fund = s.split("XX")[0] + "XX"
        fund_parts = fund.split("*")
        sym = None
        if len(fund_parts) == 2:
            sym = fund_parts[1]
        elif len(fund_parts) == 3:
            sym = fund_parts[2]
        if not sym:
            continue

        rate = s.split("XX")[1] + "%"
        dic[sym.strip().upper()] = rate.strip()

    # Keep just the ones you care about (edit list if needed)
    out = {k: v for k, v in dic.items() if k in {"POIXX", "TMCXX"}}

    _merrill_cache["ts"] = now
    _merrill_cache["data"] = out
    return out
   
# ====== ROUTES ======
@app.get("/dash", response_class=HTMLResponse)
def dash(request: Request):
    require_key(request)
    return """
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Schwab Dashboard</title>
      <style>
        body { font-family: -apple-system, Segoe UI, Arial; margin: 16px; }
        .card { border: 1px solid #ddd; border-radius: 12px; padding: 8px; margin-bottom: 8px; }
        input { font-size: 16px; padding: 6px; width: 80px; }
        button { margin: 2px; font-size: 14px; padding: 5px 10px; }
        pre { white-space: pre-wrap; word-wrap: break-word; }
        .etf-list { display: grid; gap: 6px; }
        .etf-row { display: grid; grid-template-columns: 56px 1fr; gap: 8px; align-items: center; border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px; }
        .etf-row.dim { opacity: 0.62; }
        .etf-symbol { font-weight: 700; }
        .badge { border-radius: 999px; padding: 3px 8px; font-size: 12px; font-weight: 700; }
        .badge.buy { background: #dcfce7; color: #166534; }
        .badge.strong { background: #e0f2fe; color: #075985; }
        .badge.defensive { background: #fef9c3; color: #854d0e; }
        .badge.cooldown { background: #ede9fe; color: #5b21b6; }
        .badge.neutral { background: #f3f4f6; color: #374151; }
        .badge.weak, .badge.avoid { background: #fee2e2; color: #991b1b; }
        .muted { color: #6b7280; font-size: 12px; }
      </style>
    </head>
    <body>
      <div class="card">
        <h2>Schwab Dashboard</h2>
        <div>
          <input id="sym" value="SPY" />
          <button onclick="loadDividend()">Dividend</button>
          <button onclick="loadQuote()">Quote</button>
          <button onclick="loadPositions()">Positions</button>
          <button onclick="loadTrendEtfs()">Trend Watchlist</button>
        </div>
        <p id="status"></p>
        <h3>Trend Rider Watchlist</h3>
        <div id="trendEtfs" class="etf-list"></div>
        <pre id="out"></pre>
        <pre id="posout"></pre>
      </div>

      <script>
        const key = new URLSearchParams(window.location.search).get("k");
        
        async function loadDividend() {
          document.getElementById("status").innerText = "Loading dividend/yield...";
          const r = await fetch("/api/dividend?k=" + encodeURIComponent(key));
          const j = await r.json();
          document.getElementById("status").innerText = r.ok && j.ok ? "OK" : "Error";
          document.getElementById("out").innerText = JSON.stringify(j, null, 2);
        }
        
        async function loadSWVXX() {
          document.getElementById("status").innerText = "Loading SWVXX yield...";

          const key = new URLSearchParams(window.location.search).get("k");

          const r = await fetch("/api/yield/swvxx?k=" + encodeURIComponent(key) + "&principal=60000");
          const j = await r.json();

          document.getElementById("status").innerText = r.ok ? "OK" : "Error";
          document.getElementById("out").innerText = JSON.stringify(j, null, 2);
        }

        async function loadQuote() {
          const sym = document.getElementById("sym").value.trim().toUpperCase();
          document.getElementById("status").innerText = "Loading quote...";
          const r = await fetch("/api/quote/" + sym + "?k=" + encodeURIComponent(key));
          const j = await r.json();
          document.getElementById("status").innerText = r.ok ? "OK" : "Error";
          document.getElementById("out").innerText = JSON.stringify(j, null, 2);
        }

        async function loadPositions() {
          const sym = document.getElementById("sym").value.trim().toUpperCase();
          document.getElementById("status").innerText = "Loading position...";
          const r = await fetch("/api/position/" + sym + "?k=" + encodeURIComponent(key));
          const j = await r.json();
          document.getElementById("status").innerText = r.ok ? "OK" : "Error";
          document.getElementById("posout").innerText = JSON.stringify(j, null, 2);
        }

        function renderTrendEtfs(items) {
          const box = document.getElementById("trendEtfs");
          box.innerHTML = "";
          if (!items || items.length === 0) {
            box.innerHTML = '<div class="muted">No Trend Rider watchlist symbols found.</div>';
            return;
          }
          for (const item of items || []) {
            const action = item.action || item.action_hint || "NEUTRAL";
            const row = document.createElement("div");
            row.className = "etf-row " + (action === "BUY_CANDIDATE" || action === "HOLD_STRONG" ? "" : "dim");
            const score = Number(item.score || 0).toFixed(1);
            const fromHigh = item.from_high_pct === null || item.from_high_pct === undefined ? "" : " | 52w " + item.from_high_pct + "%";
            row.innerHTML = `
              <div class="etf-symbol">${item.symbol}</div>
              <div>
                <span class="badge ${item.badge_class || "neutral"}">${action}</span>
                <div class="muted">score ${score}${fromHigh}</div>
              </div>
            `;
            box.appendChild(row);
          }
        }

        async function loadTrendEtfs() {
          document.getElementById("status").innerText = "Loading Trend Rider watchlist...";
          const r = await fetch("/api/trend-rider/watchlist?k=" + encodeURIComponent(key));
          const j = await r.json();
          document.getElementById("status").innerText = r.ok && j.ok ? "OK" : "Error";
          renderTrendEtfs(j.etfs || []);
        }
        
        
      </script>
    </body>
    </html>
    """

@app.get("/api/quote/{symbol}")
def api_quote(symbol: str, request: Request):
    require_key(request)
    raw = run_quote(symbol)

    try:
        lines = raw.splitlines()
        data = {}

        for line in lines:
            if "52 week high" in line:
                data["high_52"] = float(line.split(":")[1])
            elif "52 week low" in line:
                data["low_52"] = float(line.split(":")[1])
            elif "daily high" in line:
                data["daily_high"] = float(line.split(":")[1])
            elif "daily low" in line:
                data["daily_low"] = float(line.split(":")[1])
            elif "close price" in line:
                data["close"] = float(line.split(":")[1])
            elif "last price" in line:
                data["last"] = float(line.split(":")[1])

        if "last" in data and "high_52" in data:
            data["dollar_from_52_high"] = round((data["last"] - data["high_52"]) 
            )

        if "last" in data and "low_52" in data:
            data["dollar_from_52_low"] = round((data["last"] - data["low_52"])
            )

        return {"symbol": symbol.upper(), "data": data}

    except Exception as e:
        return {"error": str(e), "raw": raw}
        
@app.get("/api/position/{symbol}")
def api_position(symbol: str, request: Request):
    require_key(request)

    try:
        p = run_position_one(symbol)

        # Normalize keys from your script output
        # Your script prints keys like:
        # shares, price, gain/loss, gl/share, From 52 weeks high, From 52 weeks low
        shares = float(p.get("shares", 0.0))
        price = float(p.get("price", 0.0))
        gl_total = p.get("gain/loss", p.get("gl_total", None))
        gl_per_sh = p.get("gl/share", p.get("gl_per_sh", None))

        # Your script may provide 52w_high/52w_low OR may only provide "From 52 weeks ..."
        high_52 = p.get("52w_high", None)
        low_52  = p.get("52w_low", None)

        data = {
            "shares": shares,
            "last": price,           # match quote naming
            "gl_total": gl_total,
            "gl_per_sh": gl_per_sh,
            "high_52": high_52,
            "low_52": low_52,
        }

        # If we have 52-week values, compute % like your quote endpoint
        if isinstance(high_52, (int, float)) and high_52:
           data["dollar_from_52_high"] = round(price - float(high_52), 2)

        if isinstance(low_52, (int, float)) and low_52:
           data["dollar_from_52_low"] = round(price - float(low_52), 2)

        return {"symbol": symbol.upper(), "data": data}

    except Exception as e:
        return {"error": str(e)}
        
@app.get("/api/dividend")
def api_dividend(request: Request):
    require_key(request)
    try:
        yields = get_merrill_mmf_yields()
        return {
            "ok": True,
            "yields": yields,
            "cached_sec": int(time.time() - _merrill_cache["ts"]) if _merrill_cache["data"] else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/capital-readiness")
def api_capital_readiness(request: Request):
    require_key(request)
    return load_capital_readiness()

@app.get("/api/capital/utilization")
def api_capital_utilization(request: Request):
    require_key(request)
    return load_capital_utilization()

@app.get("/api/trend-rider/watchlist")
def api_trend_rider_watchlist(request: Request):
    require_key(request)
    return load_trend_rider_dashboard()


from fastapi import Query

def _tail_for_log(value: str | None, limit: int = 600) -> str:
    text = (value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]

def _positions_cache_key(symbol: str | None) -> str:
    return (symbol or "__ALL__").strip().upper() or "__ALL__"

def _clone_json_dict(data: dict) -> dict:
    return json.loads(json.dumps(data))

def _get_cached_positions(cache_key: str, stale_reason: str) -> dict | None:
    with _positions_cache_lock:
        entry = _positions_lkg_cache.get(cache_key)
        if not entry:
            return None
        cached_ts = float(entry.get("ts") or 0.0)
        data = _clone_json_dict(entry["data"])

    data["stale"] = True
    data["stale_reason"] = stale_reason
    data["cache_age_sec"] = round(time.time() - cached_ts, 2)
    return data

def _set_cached_positions(cache_key: str, data: dict) -> dict:
    fresh = _clone_json_dict(data)
    fresh["stale"] = False
    fresh["stale_reason"] = None
    fresh["cache_age_sec"] = 0.0
    with _positions_cache_lock:
        _positions_lkg_cache[cache_key] = {
            "ts": time.time(),
            "data": _clone_json_dict(fresh),
        }
    return fresh

def _fetch_positions_from_trade_server(req: urllib.request.Request, attempt: int) -> dict:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=POSITIONS_PROXY_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(
                    "[POSITIONS] "
                    f"attempt={attempt} elapsed_ms={elapsed_ms:.1f} status={getattr(resp, 'status', '?')} "
                    f"json_error={exc} body_tail={_tail_for_log(raw)}"
                )
                raise RuntimeError(f"trade_server returned invalid JSON: {exc}") from exc
            print(
                "[POSITIONS] "
                f"attempt={attempt} elapsed_ms={elapsed_ms:.1f} status={getattr(resp, 'status', '?')} ok=true"
            )
            return data
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)
        print(
            "[POSITIONS] "
            f"attempt={attempt} elapsed_ms={elapsed_ms:.1f} status={exc.code} "
            f"return_code={exc.code} stderr_tail={_tail_for_log(body)}"
        )
        raise RuntimeError(f"trade_server HTTP {exc.code}: {_tail_for_log(body, 240)}") from exc
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        print(
            "[POSITIONS] "
            f"attempt={attempt} elapsed_ms={elapsed_ms:.1f} status=error return_code=NA "
            f"stderr_tail={_tail_for_log(str(exc))}"
        )
        raise

@app.get("/api/positions")
def api_positions(
    k: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY not set")

    # iPhone uses ?k=...
    if not k or k.strip() != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    normalized_symbol = symbol.strip().upper() if symbol else None
    cache_key = _positions_cache_key(normalized_symbol)
    url = f"{TRADE_SERVER_BASE}/api/positions"
    if normalized_symbol:
        url += f"?symbol={normalized_symbol}"

    req = urllib.request.Request(
        url,
        headers={
            "X-API-KEY": API_KEY,   # forward correctly to 8080
            "Accept": "application/json",
        },
        method="GET",
    )

    acquired_fetch_lock = _positions_fetch_lock.acquire(blocking=False)
    if not acquired_fetch_lock:
        acquired_fetch_lock = _positions_fetch_lock.acquire(timeout=POSITIONS_PROXY_INFLIGHT_WAIT_SEC)
        if not acquired_fetch_lock:
            cached = _get_cached_positions(cache_key, "positions fetch already in progress")
            if cached is not None:
                print(
                    "[POSITIONS] returning stale cache because another positions fetch is still running "
                    f"cache_key={cache_key} cache_age_sec={cached.get('cache_age_sec')}"
                )
                return cached
            raise HTTPException(
                status_code=502,
                detail="positions fetch already in progress and no cached positions are available",
            )

    last_error: Exception | None = None
    try:
        total_attempts = POSITIONS_PROXY_RETRIES + 1
        for attempt in range(1, total_attempts + 1):
            try:
                data = _fetch_positions_from_trade_server(req, attempt)
                return _set_cached_positions(cache_key, data)
            except Exception as exc:
                last_error = exc
                if attempt < total_attempts:
                    time.sleep(POSITIONS_PROXY_RETRY_SLEEP_SEC)

        reason = f"positions fetch failed after {total_attempts} attempts: {_tail_for_log(str(last_error), 240)}"
        cached = _get_cached_positions(cache_key, reason)
        if cached is not None:
            print(
                "[POSITIONS] returning stale cache after fetch failure "
                f"cache_key={cache_key} cache_age_sec={cached.get('cache_age_sec')} reason={_tail_for_log(str(last_error), 240)}"
            )
            return cached
        raise HTTPException(status_code=502, detail=reason)
    finally:
        if acquired_fetch_lock:
            _positions_fetch_lock.release()

@app.get("/api/buylow")
def api_buylow(request: Request):
    require_key(request)

    now = time.time()
    with _buylow_log_cache_lock:
        cached = _buylow_log_cache.get("data")
        cached_age = now - float(_buylow_log_cache.get("ts") or 0.0)
        if cached is not None and cached_age < BUYLOW_LOG_CACHE_TTL_SEC:
            out = dict(cached)
            out["cached"] = True
            out["stale"] = False
            out["cache_age_sec"] = round(cached_age, 2)
            print(f"[LOG_SUMMARY] cached elapsed_ms=0 age_sec={cached_age:.2f}")
            return out

    start = time.perf_counter()
    cmd = [sys.executable, r"C:\temp\parse_buylow_logs.py"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=BUYLOW_LOG_TIMEOUT_SEC)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "parser failed").strip())
        data = json.loads(result.stdout)
        data["cached"] = False
        data["stale"] = False
        data["elapsed_ms"] = round(elapsed_ms, 1)
        with _buylow_log_cache_lock:
            _buylow_log_cache["ts"] = time.time()
            _buylow_log_cache["data"] = data
        print(f"[LOG_SUMMARY] generated elapsed_ms={elapsed_ms:.1f} count={data.get('count')}")
        return data
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        with _buylow_log_cache_lock:
            cached = _buylow_log_cache.get("data")
            cached_age = time.time() - float(_buylow_log_cache.get("ts") or 0.0)
        print(f"[LOG_SUMMARY] stale elapsed_ms={elapsed_ms:.1f} error={e}")
        if cached is not None:
            out = dict(cached)
            out["cached"] = True
            out["stale"] = True
            out["cache_age_sec"] = round(cached_age, 2)
            out["error"] = str(e)
            return out
        return {"ok": False, "stale": True, "cached": False, "error": str(e), "entries": [], "count": 0}

@app.get("/api/ping")
def api_ping():
    return {"ok": True, "source": "dashboard_api.py"}

@app.get("/health")
def api_health():
    return {"ok": True, "source": "dashboard_api", "root": str(ROOT)}
    
