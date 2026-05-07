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

# Phase 2 path hardening: resolve repo-local paths independently of cwd.
ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parents[1])).resolve()
CONFIG_DIR = ROOT / "config"
RUNTIME_DIR = ROOT / "runtime"
CACHE_DIR = RUNTIME_DIR / "cache"
STATE_DIR = RUNTIME_DIR / "state"

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

BUYLOW_LOG_CACHE_TTL_SEC = float(os.getenv("BUYLOW_LOG_CACHE_TTL_SEC", "25"))
BUYLOW_LOG_TIMEOUT_SEC = float(os.getenv("BUYLOW_LOG_TIMEOUT_SEC", "8"))
_buylow_log_cache = {"ts": 0.0, "data": None}
_buylow_log_cache_lock = threading.Lock()

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
        </div>
        <p id="status"></p>
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


from fastapi import Query

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

    url = f"{TRADE_SERVER_BASE}/api/positions"
    if symbol:
        url += f"?symbol={symbol.strip().upper()}"

    req = urllib.request.Request(
        url,
        headers={
            "X-API-KEY": API_KEY,   # forward correctly to 8080
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(e)
        raise HTTPException(status_code=e.code, detail=f"trade_server error: {body}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Proxy failed: {e}")

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
    return {"ok": True, "source": "dashboard.dashboard_api", "root": str(ROOT)}
    
