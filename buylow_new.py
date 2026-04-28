#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# buylow_new.py
# Snapshot: 2026-01-15 — NaN-safe CAP math + safer quote handling + keep your auto-gate

r"""
Buy-Low (staged-capable, looping, multi-symbol) with:
  • Stages from buy.dic (thr %, optional frac %, optional per-stage k for ATR)
  • Gating modes: max(threshold, ATR*K) | threshold-only | ATR-only
  • Dip baseline: previous close (default) or today’s high
  • Equity soft/hard brake (drawdown from peak)
  • Per-symbol spread ceilings (bps) with JSON override of max_slippage (fraction)
  • Partial sizing honoring cash + per-symbol exposure caps (and per-symbol min_usd)
  • Hot-reload per-symbol ATR-K overrides via atrk.json

Files (defaults):
  - config\buy.dic
  - config\sym_caps.dic
  - runtime\equity_brake.json
  - config\atrk.json
  - config\sym_overrides.json
  - runtime\daily_alloc.json
  - config\equity_budget_override.json / config\equity_budget.json

Depends on: buy_relax_kit.py, trade_logger.py, schwabdev (env app_key/app_secret + tokens.txt)
"""

import os, sys, json, time as _time, argparse, math
from contextlib import contextmanager
from datetime import datetime, date, time as ttime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Tuple, List, Dict, Any, TextIO

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- helpers from local kit ---
try:
    from buy_relax_kit import partial_size, eff_max_slippage
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from buy_relax_kit import partial_size, eff_max_slippage

# --- broker SDK ---
import schwabdev  # expects env app_key/app_secret + tokens

# --- optional logging helper (yours) ---
try:
    from trade_logger import log_event, parse_order_id, extract_fill
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from trade_logger import log_event, parse_order_id, extract_fill

# ---------- float safety ----------
def _finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except Exception:
        return False

def _f(x, default=0.0) -> float:
    return float(x) if _finite(x) else float(default)

# ---------- rotating tee ----------
class RotatingTee:
    """Tee stdout to a daily log file under log_dir. Reopens when date rolls."""
    def __init__(self, stream: TextIO, log_dir: str, base_name: str = "buylow"):
        self.stream = stream
        self.log_dir = log_dir
        self.base = base_name
        self.cur_date = None
        self.log_fh: TextIO | None = None
        os.makedirs(log_dir, exist_ok=True)
        self._roll()

    def _roll(self):
        d = datetime.now().strftime("%Y%m%d")
        if d != self.cur_date:
            if self.log_fh:
                try:
                    self.log_fh.flush(); self.log_fh.close()
                except Exception:
                    pass
            path = os.path.join(self.log_dir, f"{self.base}_{d}.log")
            self.log_fh = open(path, "a", encoding="utf-8", buffering=1)
            self.cur_date = d

    def write(self, data: str):
        self._roll()
        try:
            self.stream.write(data)
        except Exception:
            pass
        try:
            if self.log_fh:
                self.log_fh.write(data)
        except Exception:
            pass

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass
        try:
            if self.log_fh:
                self.log_fh.flush()
        except Exception:
            pass

# ---------- constants / paths ----------
APP_ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parent)).resolve()
CONFIG_DIR = APP_ROOT / "config"
RUNTIME_DIR = APP_ROOT / "runtime"
LOCKS_DIR = RUNTIME_DIR / "locks"

TOKENS_FILE  = str(CONFIG_DIR / "tokens.txt")
CALLBACK_URL = "https://127.0.0.1"
BUY_DIC_PATH = str(CONFIG_DIR / "buy.dic")
SYM_CAPS_DIC = str(CONFIG_DIR / "sym_caps.dic")
EQUITY_BRAKE_FILE = str(RUNTIME_DIR / "equity_brake.json")
STAGES_DIR   = str(LOCKS_DIR)
ATRK_OVERRIDES_FILE = str(CONFIG_DIR / "atrk.json")
SYM_OVERRIDES_FILE  = str(CONFIG_DIR / "sym_overrides.json")
DAILY_ALLOC_FILE    = str(RUNTIME_DIR / "daily_alloc.json")
BUDGET_OVERRIDE_FILES = [
    str(CONFIG_DIR / "equity_budget_override.json"),
    str(CONFIG_DIR / "equity_budget.json"),
    r".\equity_budget_override.json",
    r".\equity_budget.json",
]

TZ = ZoneInfo("America/Detroit")
START_REG  = ttime(9, 30)
END_REG    = ttime(16, 0)
START_EXT  = ttime(4, 0)
END_EXT    = ttime(20, 0)

CONNECT_TIMEOUT = 10
READ_TIMEOUT    = 30
CASH_BUFFER     = 25.00

# Basis-point ceilings (code defaults). JSON eff_max_slippage() can raise via FRACTION.
SPREAD_LIMIT_BPS = {
    "DEFAULT": 10, "EETH": 30, "IBIT": 20,
    "NVDA": 8, "QQQ": 6, "SPY": 5,
    "NIO": 12, "CRCL": 12, "BIDU": 10,
    "DTE": 10, "FIG": 8
}
DEFAULT_REGIME_SYMBOL = "SPY"
ATR_LEN   = 14

_last_trade_ts: Dict[str, float] = {}

# ---------- ATR-K overrides (hot reload each pass) ----------
def load_atrk_overrides(path: str) -> Dict[str, float]:
    """Return {SYMBOL: k, 'DEFAULT': k} from JSON; empty dict if missing/invalid."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, float] = {}
        for k, v in raw.items():
            if v is None:
                continue
            try:
                out[(k.upper() if isinstance(k, str) else k)] = float(v)
            except Exception:
                continue
        return out
    except Exception:
        return {}

# ---------- utils ----------
def session_with_retries() -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=5, connect=3, read=3, backoff_factor=0.6,
        status_forcelist=[429,500,502,503,504],
        allowed_methods=["GET","POST"]
    )))
    return s

def parse_json(resp_or_text):
    if hasattr(resp_or_text, "json"):
        try: return resp_or_text.json()
        except Exception: pass
    return json.loads(getattr(resp_or_text, "text", resp_or_text))

@contextmanager
def file_lock(path, timeout=10):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    start = _time.time()
    locked = False
    try:
        while _time.time() - start < timeout:
            try:
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True; break
            except OSError:
                _time.sleep(0.2)
        if not locked:
            raise TimeoutError(f"Could not obtain lock: {path}")
        yield
    finally:
        if locked:
            try:
                fh.seek(0)
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        fh.close()

def in_window(now, hours: str = 'regular') -> bool:
    """Return True if now is inside the allowed trading window."""
    if now.weekday() >= 5:
        return False
    h = (hours or '').lower()
    if h.startswith('ext'):
        return START_EXT <= now.time() < END_EXT
    return START_REG <= now.time() < END_REG

# ---------- Budget / Daily Alloc helpers ----------
def _try_load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}

def _budget_override_from_anywhere() -> float | None:
    envv = os.getenv("BUDGET_USD_OVERRIDE")
    if envv:
        try:
            return float(envv)
        except ValueError:
            pass
    for p in BUDGET_OVERRIDE_FILES:
        d = _try_load(p)
        for k in ("budget_usd", "budget", "remaining_usd", "remaining"):
            v = d.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    return None

def _normalize_acct_str(s: str) -> str:
    return "".join(ch for ch in str(s) if s and ch.isdigit())

def _last4(s: str) -> str:
    n = _normalize_acct_str(s);  return n[-4:] if len(n) >= 4 else n

def _daily_remaining_usd(active_acct_last4: str | None = None) -> float:
    d = _try_load(DAILY_ALLOC_FILE)
    if not d:
        return float("inf")
    try:
        today = str(date.today())
        dstr = str(d.get("date", today))
        if dstr[:10] != today:
            pass
    except Exception:
        pass
    acct_in_file = str(d.get("account") or "")
    if active_acct_last4 and acct_in_file and (active_acct_last4 not in acct_in_file):
        pass
    total = d.get("total_usd", d.get("total", None))
    remaining = d.get("remaining_usd", d.get("remaining", None))
    consumed_total = d.get("consumed_total", None)
    if remaining is None:
        if consumed_total is None:
            cons = d.get("consumed", {})
            consumed_total = sum(v for v in cons.values() if isinstance(v, (int, float)))
        try:
            if total is None:
                return float("inf")
            remaining = max(0.0, float(total) - float(consumed_total or 0.0))
        except Exception:
            remaining = float("inf")
    try:
        return float(remaining)
    except Exception:
        return float("inf")

def _consume_daily(amount_usd: float) -> None:
    if amount_usd <= 0:
        return
    try:
        lock_path = DAILY_ALLOC_FILE + ".lock"
        with file_lock(lock_path, timeout=10):
            d = _try_load(DAILY_ALLOC_FILE)
            if not d:
                return
            total = d.get("total_usd", d.get("total", 0.0)) or 0.0
            remaining = d.get("remaining_usd", d.get("remaining", None))
            consumed_total = d.get("consumed_total", None)
            if remaining is None:
                if consumed_total is None:
                    consumed_total = sum(v for v in (d.get("consumed", {}) or {}).values() if isinstance(v, (int,float)))
                remaining = max(0.0, float(total) - float(consumed_total or 0.0))
            remaining = max(0.0, float(remaining) - float(amount_usd))
            d["remaining_usd"] = remaining
            d["remaining"] = remaining
            d["consumed_total"] = float(total) - remaining
            d["last_update"] = datetime.now(TZ).isoformat()
            with open(DAILY_ALLOC_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
    except Exception:
        pass

def _broker_order_accepted(resp) -> tuple[bool, int | None, str]:
    status_code = getattr(resp, "status_code", None)
    order_id = parse_order_id(resp)
    ok = bool(status_code is not None and 200 <= int(status_code) < 300 and order_id)
    return ok, status_code, order_id

def _load_sym_min_usd(symbol: str, cli_min_usd: float) -> float:
    try:
        d = _try_load(SYM_OVERRIDES_FILE)
        default_min = d.get("DEFAULT", {}).get("min_usd", None) if isinstance(d.get("DEFAULT", {}), dict) else None
        sym_min = d.get(symbol.upper(), {}).get("min_usd", None) if isinstance(d.get(symbol.upper(), {}), dict) else None
        for v in (sym_min, default_min, cli_min_usd):
            if v is not None:
                return float(v)
    except Exception:
        pass
    return float(cli_min_usd or 0.0)

def _log_budget_why(logger_print,
                    *, cash_after_reserve: float,
                    sum_headroom: float,
                    daily_remaining: float,
                    brake_budget: float,
                    global_cap_gap: float,
                    min_usd: float,
                    dd: float,
                    brake_on: bool) -> float:
    parts = {
        "cash_after_reserve": round(cash_after_reserve, 2),
        "sum_headroom": round(sum_headroom, 2),
        "daily_remaining": (round(daily_remaining, 2) if math.isfinite(daily_remaining) else "inf"),
        "brake_budget": (round(brake_budget, 2) if math.isfinite(brake_budget) else "inf"),
        "global_cap_gap": round(global_cap_gap, 2),
        "min_usd": round(min_usd, 2),
        "dd": f"{dd:.2%}",
        "brake_on": bool(brake_on),
    }
    eff = min(
        cash_after_reserve,
        sum_headroom,
        (daily_remaining if math.isfinite(daily_remaining) else float("inf")),
        (brake_budget if math.isfinite(brake_budget) else float("inf")),
        global_cap_gap
    )
    ov = _budget_override_from_anywhere()
    if ov is not None:
        parts["override_budget"] = round(float(ov), 2)
        eff = min(eff, float(ov))
    logger_print(f"[WHY] budget_parts={parts} -> effective_budget={eff:.2f}")
    return eff

# ---------- light TA ----------
def sma(vals, n):
    if len(vals) < n: return None
    return sum(vals[-n:]) / n

def atr(candles, n=14):
    if len(candles) < n+1: return None
    trs, prev_close = [], candles[-(n+1)]["close"]
    for c in candles[-n:]:
        hi_lo = c["high"] - c["low"]
        hi_cl = abs(c["high"] - prev_close)
        lo_cl = abs(c["low"] - prev_close)
        trs.append(max(hi_lo, hi_cl, lo_cl))
        prev_close = c["close"]
    return sum(trs)/n

# ---------- Schwab helpers ----------

def detect_direction_change(client, symbol: str) -> tuple[bool, str]:
    """
    Detect short-term reversal behavior.
    Returns (triggered, reason)
    """
    try:
        candles = get_daily_history(client, symbol)
    except Exception:
        return False, ""

    if not isinstance(candles, list) or len(candles) < 4:
        return False, ""

    try:
        c1 = candles[-3]
        c2 = candles[-2]
        c3 = candles[-1]

        if not all(isinstance(c, dict) for c in (c1, c2, c3)):
            return False, ""

        for c in (c1, c2, c3):
            for k in ("open", "high", "low", "close"):
                if k not in c:
                    return False, ""

        c1_close = float(c1["close"])
        c2_close = float(c2["close"])
        c2_low = float(c2["low"])
        c3_open = float(c3["open"])
        c3_close = float(c3["close"])
        c3_high = float(c3["high"])
        c3_low = float(c3["low"])

        if c1_close <= 0 or c2_close <= 0 or c3_low <= 0:
            return False, ""

        # --- Trigger A: Drop + stabilize ---
        drop1 = (c2_close - c1_close) / c1_close
        drop2 = (c3_close - c2_close) / c2_close

        if drop1 < -0.01 and drop2 < 0:
            if c3_close > c3_open and c3_low >= c2_low:
                return True, "drop+stabilize"

        # --- Trigger B: Intraday reversal ---
        intraday_range = (c3_high - c3_low) / c3_low
        if intraday_range > 0.015 and c3_close > c3_open:
            return True, "intraday_reversal"

        return False, ""
    except Exception:
        return False, ""

def warn_if_refresh_stale(tokens_path=TOKENS_FILE, days=6):
    try:
        with open(tokens_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        issued = data.get("refresh_token_issued")
        if issued:
            t0 = datetime.fromisoformat(issued.replace("Z","+00:00"))
            age = datetime.now(timezone.utc) - t0
            if age >= timedelta(days=days):
                print(f"[WARN] Refresh token is {age.days} days old. Re-auth soon.")
    except Exception:
        pass

def _load_acct_map(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict):
            return {k.upper(): str(v) for k, v in m.items()}
    except Exception:
        pass
    return {}

def select_account_hash(client, acct_arg: str, acct_file: str) -> tuple[str, str]:
    wanted = (acct_arg or "").strip()
    if not wanted:
        raise RuntimeError("--acct is required")
    acct_map = _load_acct_map(acct_file)
    if wanted.upper() in acct_map:
        wanted = acct_map[wanted.upper()]
    wanted_norm  = _normalize_acct_str(wanted)
    wanted_last4 = _last4(wanted_norm)

    linked = parse_json(client.account_linked())
    candidates = []
    for node in (linked or []):
        accnum = node.get("accountNumber") or node.get("accountId") or node.get("number") or ""
        disp   = node.get("displayName") or node.get("description") or node.get("accountName") or accnum
        hval   = node.get("hashValue") or node.get("hash") or node.get("accountHash")
        if not hval:
            continue
        accnum_norm = _normalize_acct_str(accnum)
        score = 0
        if accnum_norm and accnum_norm == wanted_norm: score = 100
        elif accnum_norm and _last4(accnum_norm) == wanted_last4: score = 80
        elif wanted.upper() == (disp or "").upper(): score = 60
        candidates.append((score, hval, accnum, disp))
    if not candidates:
        raise RuntimeError("No linked accounts found.")
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0]
    acct_hash = best[1]
    label = f"{best[2]} {best[3]}".strip()
    return acct_hash, label

def positions_payload(account_hash: str, access_token: str) -> dict:
    url = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}"
    r = session_with_retries().get(
        url, params={"fields":"positions"},
        headers={"Accept":"application/json","Authorization":f"Bearer {access_token}"},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    r.raise_for_status()
    return parse_json(r)

def _all_positions_list(payload) -> list:
    positions = []
    if isinstance(payload, dict):
        sa = payload.get("securitiesAccount", {})
        positions = sa.get("positions", []) or []
    elif isinstance(payload, list):
        for acct in payload:
            sa = acct.get("securitiesAccount", {})
            positions.extend(sa.get("positions", []) or [])
    return positions

def get_long_qty(account_hash: str, access_token: str, symbol: str) -> float:
    data = positions_payload(account_hash, access_token)
    positions = _all_positions_list(data)
    sym = symbol.upper()
    qty = 0.0
    for p in positions:
        instr = p.get("instrument", {})
        if (instr.get("symbol") or "").upper() != sym:
            continue
        if p.get("longQuantity") is not None:
            qty += float(p["longQuantity"])
        elif p.get("quantity") is not None:
            qty += float(p["quantity"])
    return qty

def get_account_cash_and_equity(account_hash: str, access_token: str) -> tuple[float,float]:
    url = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}"
    r = session_with_retries().get(
        url, params={"fields":"positions"},
        headers={"Accept":"application/json","Authorization":f"Bearer {access_token}"},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    r.raise_for_status()
    data = parse_json(r)
    acct = data.get("securitiesAccount", data) if isinstance(data, dict) else (
        data[0].get("securitiesAccount", {}) if isinstance(data, list) and data else {}
    )
    balances = acct.get("currentBalances", {})
    cash = None
    for key in ("cashAvailableForTrading","cashBalance","availableFunds"):
        v = balances.get(key)
        if v is not None: cash = float(v); break
    equity = None
    for k in ("liquidationValue","equity","accountValue"):
        v = balances.get(k)
        if v is not None: equity = float(v); break
    return (cash or 0.0), (equity or 0.0)

# ---------- history/ATR/regime ----------
def _history_via_client(client, symbol):
    try:
        return parse_json(client.price_history(symbol, period_type="year", period=2, frequency_type="daily", frequency=1))
    except Exception:
        return None

def _history_via_http(client, symbol):
    url = f"https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {"symbol":symbol,"periodType":"year","period":"2","frequencyType":"daily","frequency":"1","needExtendedHoursData":"false"}
    headers = {"Authorization":f"Bearer {client.access_token}","Accept":"application/json"}
    r = session_with_retries().get(url, params=params, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    if r.status_code == 200:
        return parse_json(r)
    return None

def get_daily_history(client, symbol):
    data = _history_via_client(client, symbol) or _history_via_http(client, symbol)
    candles = (data or {}).get("candles") or (data or {}).get("data") or []
    norm = []
    for c in candles:
        if isinstance(c, dict) and all(k in c for k in ("open","high","low","close")):
            try:
                norm.append({"open": float(c["open"]), "high": float(c["high"]),
                             "low": float(c["low"]), "close": float(c["close"])})
            except Exception:
                pass
    return norm

def get_regime_and_atr(client, symbol_for_regime, symbol_for_atr=None):
    sym_hist = get_daily_history(client, symbol_for_regime)
    if not sym_hist or len(sym_hist) < 220:
        return (True, None, None)
    closes = [c["close"] for c in sym_hist]
    sma200 = sma(closes, 200); sma50 = sma(closes, 50)
    regime_up = (closes[-1] > (sma200 or closes[-1])) and (sma50 or closes[-1]) > (sma200 or closes[-1])
    atr_sym = symbol_for_atr or symbol_for_regime
    atr_hist = sym_hist if atr_sym == symbol_for_regime else get_daily_history(client, atr_sym)
    daily_atr = atr(atr_hist or sym_hist, ATR_LEN)
    prev_close = closes[-2] if len(closes) >= 2 else closes[-1]
    return (regime_up, (daily_atr or None), prev_close)

# ---------- equity brake ----------
EQUITY_BRAKE_LOCK = EQUITY_BRAKE_FILE + ".lock"

def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def load_equity_brake() -> dict:
    try:
        with file_lock(EQUITY_BRAKE_LOCK, timeout=2):
            d = _read_json(EQUITY_BRAKE_FILE)
    except Exception:
        d = _read_json(EQUITY_BRAKE_FILE)
    d.setdefault("peak", 0.0)
    d.setdefault("brake_on", False)
    d.setdefault("brake_level", "none")
    d.setdefault("last_update", datetime.now(timezone.utc).isoformat())
    return d

def save_equity_brake(d: dict) -> None:
    d["last_update"] = datetime.now(timezone.utc).isoformat()
    tmp = EQUITY_BRAKE_FILE + ".tmp"
    try:
        with file_lock(EQUITY_BRAKE_LOCK, timeout=2):
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
            os.replace(tmp, EQUITY_BRAKE_FILE)
    except Exception:
        with open(EQUITY_BRAKE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)

def update_brake_state(current_equity: float, regime_up: bool, *,
                        soft_brake_pct: float | None = None,
                        hard_brake_pct: float | None = None,
                        single_brake_pct: float | None = None) -> Tuple[bool, str, float]:
    st = load_equity_brake()
    if current_equity and current_equity > st.get("peak", 0.0):
        st["peak"] = float(current_equity)
    peak = st.get("peak", 0.0) or 0.0
    dd_down = 0.0
    if peak > 0 and current_equity is not None:
        dd_down = max(0.0, (peak - float(current_equity)) / peak)
    old_level = st.get("brake_level", "none")
    if soft_brake_pct is not None or hard_brake_pct is not None:
        soft_thr = (soft_brake_pct or 1e9) / 100.0
        hard_thr = (hard_brake_pct or 1e9) / 100.0
        if dd_down >= hard_thr:
            st["brake_on"], st["brake_level"] = True, "hard"
        elif dd_down >= soft_thr:
            st["brake_on"], st["brake_level"] = True, "soft"
        else:
            if regime_up:
                st["brake_on"], st["brake_level"] = False, "none"
    else:
        thr = ((single_brake_pct or 10.0) / 100.0)
        if dd_down >= thr:
            st["brake_on"], st["brake_level"] = True, "soft"
        else:
            if regime_up:
                st["brake_on"], st["brake_level"] = False, "none"
    if st.get("brake_level") != old_level or True:
        save_equity_brake(st)
    return (st["brake_on"], st["brake_level"], dd_down)

# ---------- buy.dic loaders (staged) ----------
def _load_json_utf8_or_sig(path: str):
    try:
        return json.loads(open(path, "r", encoding="utf-8").read())
    except UnicodeDecodeError:
        return json.loads(open(path, "r", encoding="utf-8-sig").read())

def _norm_stage_item(x) -> Dict[str, Any] | None:
    if isinstance(x, (int, float)):
        return {"thr": float(x), "frac": None, "k": None}
    if isinstance(x, dict):
        thr  = x.get("thr") or x.get("thr_pct") or x.get("threshold_pct")
        frac = x.get("frac") or x.get("qty_pct")
        k    = x.get("k") or x.get("K") or x.get("atr_k")
        thrv = None
        if thr is not None:
            try: thrv = float(thr)
            except Exception: pass
        kf = None
        if k is not None:
            try: kf = float(k)
            except Exception: pass
        ff = None
        if frac is not None:
            try: ff = float(frac)
            except Exception: pass
        return {"thr": thrv, "frac": ff, "k": kf}
    return None

def load_buy_stages(symbol: str, buy_dic_path: str) -> List[Dict[str, Any]]:
    try:
        raw = _load_json_utf8_or_sig(buy_dic_path)
    except Exception:
        print(f"[CONF] buy.dic load failed; DEFAULT=0.5%", flush=True)
        return [{"thr": 0.5, "frac": None, "k": None}]
    up = {(k.upper() if isinstance(k,str) else k): v for k, v in raw.items()}
    v = up.get(symbol.upper(), up.get("DEFAULT", 0.5))
    if isinstance(v, list):
        out: List[Dict[str, Any]] = []
        for it in v:
            itn = _norm_stage_item(it)
            if itn: out.append(itn)
        out.sort(key=lambda z: (float('inf') if z["thr"] is None else z["thr"]))
        use = out if out else [{"thr": 0.5, "frac": None, "k": None}]
    else:
        itn = _norm_stage_item(v)
        use = [itn] if itn else [{"thr": 0.5, "frac": None, "k": None}]
    try:
        if isinstance(v, list):
            info = ", ".join(
                (f"{(it['thr'] if it['thr'] is not None else 0):.1f}%"
                 + (f"/k={it['k']:.2f}" if it.get('k') is not None else "")
                 + (f"/frac={it['frac']:.2f}" if it.get('frac') is not None else ""))
                for it in use
            )
            print(f"[CONF] buy.dic -> {symbol.upper()} stages: [{info}] (file={buy_dic_path})", flush=True)
        else:
            it = use[0]
            info = f"{(it['thr'] if it['thr'] is not None else 0):.1f}%"
            if it.get("k") is not None: info += f"/k={it['k']:.2f}"
            if it.get("frac") is not None: info += f"/frac={it['frac']:.2f}"
            print(f"[CONF] buy.dic -> {symbol.upper()}={info} (file={buy_dic_path})", flush=True)
    except Exception:
        pass
    return use

# ---------- sym caps loader ----------
def load_sym_cap(symbol: str, fallback: float) -> float:
    try:
        with open(SYM_CAPS_DIC, "r", encoding="utf-8") as f:
            m = json.load(f)
        return float(m.get(symbol.upper(), m.get("DEFAULT", fallback)))
    except Exception:
        return float(fallback)

# ---------- stage state ----------
def _stage_state_path(symbol: str) -> str:
    return os.path.join(STAGES_DIR, f"STAGES_{symbol.upper()}.json")

def _read_stage_state(symbol: str) -> dict:
    p = _stage_state_path(symbol)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"date": str(date.today()), "fired": []}

def _write_stage_state(symbol: str, st: dict):
    p = _stage_state_path(symbol)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, p)

# ---------- core run ----------
def _run_unlocked(symbol: str,
        usd: float,
        confirm: bool,
        *,
        atr_k: float = 1.5,
        dd_brake_pct: float = 10.0,
        exp_cap: float = 0.60,
        sym_cap: float = 0.05,
        regime_symbol: str = DEFAULT_REGIME_SYMBOL,
        order_style: str = "limit",
        max_slippage: float = 0.003,
        hours: str = "regular",
        soft_brake: float | None = None,
        hard_brake: float | None = None,
        brake_verbose: bool = False,
        strict_atr: bool = False,
        no_spread_override: bool = False,
        min_qty: int = 1,
        min_usd: float = 0.0,
        args_acct: str = "",
        args_acct_file: str = str(CONFIG_DIR / "acct.json"),
        buy_dic: str = BUY_DIC_PATH,
        reset_stages: bool = False,
        stage_expire_days: int = 3,
        batch_stages: bool = False,
        gate_mode: str = "max",
        dip_baseline: str = "prevclose",
        atrk_file_map: Dict[str, float] | None = None,
        atrk_map_cli: Dict[str, float] | None = None,
        atrk_uniform_cli: float = 1.5):

    stock = symbol.upper()

    app_key = os.getenv("app_key"); app_secret = os.getenv("app_secret")
    if not app_key or not app_secret:
        print("[ERR] Set env vars app_key/app_secret before running.")
        return

    client = schwabdev.Client(app_key, app_secret, CALLBACK_URL, TOKENS_FILE)
    try:
        with file_lock(TOKENS_FILE + ".lock"):
            client.update_tokens()
    except Exception:
        print("[ERROR] token refresh failed — reauth likely required.")

    warn_if_refresh_stale(TOKENS_FILE, days=6)

    try:
        account_hash, acct_label = select_account_hash(client, args_acct, args_acct_file)
        print(f"[ACCT] Using account: {acct_label} (hash={account_hash})")
    except Exception as e:
        print("[ERROR] Could not select account:", e)
        return

    cash, equity = get_account_cash_and_equity(account_hash, client.access_token)

    regime_up, daily_atr, prev_close_regime = get_regime_and_atr(client, regime_symbol, symbol_for_atr=stock)

    brake_on, brake_level, dd_down = update_brake_state(
        equity, regime_up,
        soft_brake_pct=soft_brake,
        hard_brake_pct=hard_brake,
        single_brake_pct=dd_brake_pct
    )

    if brake_verbose:
        peak_dbg = load_equity_brake().get('peak', 0)
        print(f"[BRAKE] dd={dd_down:.2%} level={brake_level} on={brake_on} peak={peak_dbg:.2f}")

    if brake_on:
        print(f"[BRAKE] {brake_level.upper()} active — new buys paused.")
        return

    # quote (NaN-safe)
    try:
        q = parse_json(client.quote(stock))
        node = q[stock]["quote"]
        last = node.get("lastPrice")
        close = node.get("closePrice")
        ask = node.get("askPrice")
        if not _finite(ask):
            ask = last
        if not _finite(ask):
            ask = close
        last = _f(last, ask)
        close = _f(close, last)
        ask = _f(ask, last)
    except Exception as e:
        print(f"[WARN] Could not get quote for {stock}:", e)
        return

    if not _finite(close) or not _finite(ask) or close <= 0 or ask <= 0:
        print(f"[WARN] Missing/invalid close/ask for {stock}; close={close} ask={ask}; skipping.")
        return

    # spread info + allowances
    def spread_info(qnode: dict):
        bid, askp = qnode.get("bidPrice"), qnode.get("askPrice")
        bidf = _f(bid, 0.0)
        askf = _f(askp, 0.0)
        if bidf > 0 and askf > 0 and askf >= bidf:
            mid = (bidf + askf) / 2.0
            bps = (askf - bidf) / mid * 1e4
            return bidf, askf, mid, bps
        return bidf, askf, None, float("inf")

    bid, askp, mid, bps = spread_info(node)
    limit_bps_code = SPREAD_LIMIT_BPS.get(stock, SPREAD_LIMIT_BPS["DEFAULT"])

    try:
        override_frac = eff_max_slippage(stock, max_slippage)
    except Exception:
        print(f"[INFO] {stock} slippage override unavailable; using CLI max_slippage={max_slippage:.4f}")
        override_frac = max_slippage

    override_bps = int(round((_f(override_frac, 0.0)) * 1e4))
    allowed_bps = max(limit_bps_code, override_bps)
    
    # exposure cap (portfolio gross long)
   
    def estimate_gross_long_mv(account_hash: str, access_token: str) -> float:
        payload = positions_payload(account_hash, access_token)
        mv = 0.0

        EXCLUDE_FROM_GROSS_CAP = {"SWVXX"}   # 👈 key fix

        for p in _all_positions_list(payload):
            instr = p.get("instrument", {}) or {}
            sym = str(instr.get("symbol") or "").upper()

            if sym in EXCLUDE_FROM_GROSS_CAP:
                continue

            mv += _f(p.get("marketValue"), 0.0)

        return mv

    gross_mv = estimate_gross_long_mv(account_hash, client.access_token)
    if _finite(equity) and equity > 0 and (gross_mv / equity) >= exp_cap * 0.999:
        print(f"[CAP] Gross long {gross_mv/equity:.1%} >= cap {exp_cap:.0%}; skip.")
        return

    try:
        qty_owned = get_long_qty(account_hash, client.access_token, stock)
    except Exception as e:
        print(f"[WARN] Could not fetch positions for {stock}:", e)
        qty_owned = 0.0
    qty_owned = _f(qty_owned, 0.0)

    sym_cap_eff = load_sym_cap(stock, sym_cap)
    sym_mv_cap  = sym_cap_eff * equity if _finite(equity) and equity > 0 else float('inf')

    # -------- staged thresholds (with per-stage k) --------
    stages = load_buy_stages(stock, buy_dic)
    N = len(stages)

    prev_close = _f(node.get("closePrice") or prev_close_regime or close, close)
    today_high = node.get("highPrice")
    if (dip_baseline or "prevclose").lower().startswith("today") and _finite(today_high) and float(today_high) > 0:
        base_price = float(today_high)
        baseline_name = "todayhigh"
    else:
        base_price = float(prev_close)
        baseline_name = "prevclose"

    def k_effective(stage_k: float | None) -> float:
        if stage_k is not None and _finite(stage_k):
            return float(stage_k)
        su = stock.upper()
        if isinstance(atrk_file_map, dict) and su in atrk_file_map:
            return float(atrk_file_map[su])
        if isinstance(atrk_file_map, dict) and "DEFAULT" in atrk_file_map:
            return float(atrk_file_map["DEFAULT"])
        if isinstance(atrk_map_cli, dict) and su in atrk_map_cli:
            return float(atrk_map_cli[su])
        return float(atrk_uniform_cli if atrk_uniform_cli is not None else atr_k)

    def compute_dip_needed(atr_dollar: float | None, k_val: float, thr_pct: float | None, base_px: float) -> float:
        thr_floor = (float(thr_pct) / 100.0) * float(base_px) if (thr_pct is not None) else 0.0
        atr_floor = float(k_val) * float(atr_dollar or 0.0)
        mode = (gate_mode or "max").lower()
        if mode == "thr":
            return thr_floor
        elif mode == "atr":
            return atr_floor
        return max(thr_floor, atr_floor)

    def stage_target(base_px: float, atr_dollar: float | None, k_val: float, thr_pct: float | None) -> float:
        return float(base_px) - compute_dip_needed(atr_dollar, k_val, thr_pct, base_px)

    stage_targets: List[Tuple[int, float, float]] = []
    ks_used: Dict[int, float] = {}
    for idx, st in enumerate(stages):
        k_eff = k_effective(st.get('k'))
        ks_used[idx] = k_eff
        thr_val = st.get('thr')  # may be None
        tpx = stage_target(base_price, daily_atr, k_eff, thr_val)
        stage_targets.append((idx, (0.0 if thr_val is None else float(thr_val)), tpx))

    st_state = _read_stage_state(stock)
    today_s = str(date.today())

    if reset_stages:
        st_state = {"date": today_s, "fired": []}
    else:
        try:
            last_dt = datetime.fromisoformat(st_state.get("date", today_s))
            if (date.today() - last_dt.date()).days > int(stage_expire_days or 0):
                st_state = {"date": today_s, "fired": []}
        except Exception:
            st_state = {"date": today_s, "fired": []}

    fired = set(int(x) for x in st_state.get('fired', []))

    met_indices = [i for (i, _, tpx) in stage_targets if ask <= tpx]
    eligible_unfired = [i for i in met_indices if (i+1) not in fired]
    to_fire = eligible_unfired[:] if batch_stages else eligible_unfired[:1]
    meets_target = len(to_fire) > 0

    if not meets_target:
        nf = 0
        while (nf < N) and ((nf+1) in fired):
            nf += 1
        look_i = nf if nf < N else (N-1)
        _, use_thr_val, use_tp = stage_targets[look_i]
        use_k = ks_used.get(look_i, k_effective(None))
        stage_info = (f"stage next={(nf+1) if nf < N else N}/{N} "
                      f"thr={use_thr_val:.2f}% k={use_k:.2f} target={use_tp:.2f} fired={sorted(fired)}")
    else:
        hi = to_fire[-1]
        _, use_thr_val, use_tp = stage_targets[hi]
        use_k = ks_used.get(hi, k_effective(None))
        stage_info = (f"stages firing={[x+1 for x in to_fire]} of {N}; "
                      f"last thr={use_thr_val:.2f}% k={use_k:.2f} target={use_tp:.2f} fired={sorted(fired)}")

    # headline (NaN-safe)
    denom_base = base_price if _finite(base_price) and base_price != 0 else ask
    denom_pc   = prev_close if _finite(prev_close) and prev_close != 0 else ask
    dip_from_base      = 100.0 * (base_price - ask) / denom_base
    dip_from_prevclose = 100.0 * (prev_close - ask) / denom_pc

    print(f"{stock} ask={ask:.4f} base={base_price:.4f}({baseline_name}) prev_close={prev_close:.4f} "
          f"dip_base={dip_from_base:.2f}% dip_pc={dip_from_prevclose:.2f}% ATR$={_f(daily_atr,0):.4f} "
          f"gate={gate_mode} {stage_info} -> {'BUY' if (regime_up and meets_target) else 'hold'}")
          

    # --- Near-trigger alert (SWVXX prep signal) ---
    near_trigger_pct = 2.0 if stock == "QQQ" else 1.5   # % above target to trigger alert

    if not meets_target and _finite(ask) and _finite(use_tp) and use_tp > 0:
        try:
            above_target_pct = ((ask / use_tp) - 1.0) * 100.0

            usable_cash = max(0.0, float(cash) - float(CASH_BUFFER))
            if 0 <= above_target_pct <= near_trigger_pct and usable_cash > 100.0:

                # Determine next stage index safely
                nf_local = locals().get("nf", 0)
                next_stage_idx = min(nf_local, N - 1)

                # Determine correct stage fraction
                if stages and stages[next_stage_idx].get("frac") is not None:
                    stage_frac = float(stages[next_stage_idx]["frac"])
                elif N > 1:
                    stage_frac = 1.0 / N
                else:
                    stage_frac = 1.0

                est_stage_usd = min(
                    max(0.0, float(usd or 0.0)) * stage_frac,
                    usable_cash
                )

                print(
                    f"[NEAR-TRIGGER] {stock} within {above_target_pct:.2f}% of target "
                    f"({ask:.2f} vs {use_tp:.2f}) → Consider selling SWVXX ≈ ${est_stage_usd:,.0f}"
                )

        except Exception as e:
            print(f"[WARN] near-trigger calc failed: {e}")

    # ---------- CAP diagnostics snapshot (pre-orders) — NaN-safe ----------
    _est_raw = ask if _finite(ask) else (close if _finite(close) else 0.0)
    _slip    = _f(max_slippage, 0.0)
    _est_price = _est_raw * (1.0 + max(0.0, _slip))

    if _finite(_est_price) and _est_price > 0:
        _sym_mv_cap = _f(sym_mv_cap, 0.0) if (math.isfinite(sym_mv_cap) if isinstance(sym_mv_cap, float) else True) else 0.0
        _qty_owned  = _f(qty_owned, 0.0)
        _current_mv = _qty_owned * _est_price

        if math.isfinite(sym_mv_cap):
            _allow_mv = max(0.0, float(sym_mv_cap) - _current_mv)
            _max_shares = int(_allow_mv / _est_price) if _allow_mv > 0 else 0
        else:
            _allow_mv = float("inf")
            _max_shares = 10**9  # effectively unbounded

        print(f"[CAP-DETAIL] sym_cap={sym_cap_eff:.1%} equity={equity:.2f} cap_mv={sym_mv_cap if math.isfinite(sym_mv_cap) else float('inf')} "
              f"curr_mv={_current_mv:.2f} headroom={_allow_mv if math.isfinite(_allow_mv) else float('inf')} "
              f"max_new_shares={_max_shares} target={use_tp:.2f}")
    else:
        print(f"[CAP-DETAIL] {stock} skipped (est_price invalid) ask={ask} close={close} max_slippage={max_slippage}")

    target_gap_dollar = (ask - use_tp) if (_finite(ask) and _finite(use_tp)) else float("nan")
    target_gap_pct = ((ask / use_tp) - 1.0) * 100.0 if (_finite(ask) and _finite(use_tp) and use_tp) else float("nan")
    cap_headroom = _allow_mv if "_allow_mv" in locals() else float("nan")
    if not regime_up:
        primary_block = "regime"
    elif not meets_target:
        primary_block = "target"
    elif bps > allowed_bps:
        primary_block = "spread"
    elif _finite(cap_headroom) and cap_headroom <= 0:
        primary_block = "cap"
    else:
        primary_block = "ready"
    print(
        f"[SUMMARY] {stock} signal={'BUY' if (regime_up and meets_target) else 'HOLD'} "
        f"block={primary_block} dip={dip_from_base:.2f}% "
        f"target_gap=${target_gap_dollar:.2f} ({target_gap_pct:.2f}%) "
        f"spread_bps={bps:.1f}/{allowed_bps} cap_headroom=${cap_headroom:.2f} "
        f"brake={brake_level}"
    )

    # log signal
    log_event(side="BUY", symbol=stock, mode="buy", baseline=baseline_name,
              threshold_pct=use_thr_val, last=(last or ""), close=close,
              dip_pct=round(dip_from_base, 4), trigger=bool(regime_up and meets_target),
              action="SIGNAL", notes=f"stage_info={stage_info}; gate={gate_mode}")

    # Spread and strict-atr gates
    if bps > allowed_bps:
        if no_spread_override:
            print(f"[SPREAD-BLOCK] {stock} spread {bps:.1f} bps > allowed {allowed_bps} bps "
                  f"(code={limit_bps_code} | json={override_bps}) -> HOLD.")
            return
        else:
            if not meets_target:
                print(f"[SKIP] {stock} spread {bps:.1f} bps > allowed {allowed_bps} bps and ask above target.")
                return
            print(f"[WARN] {stock} spread {bps:.1f} bps > allowed {allowed_bps} bps "
                  f"(code={limit_bps_code} | json={override_bps}); override permitted; proceeding.")
    else:
        print(f"[OK] {stock} spread {bps:.1f} bps ≤ allowed {allowed_bps} bps (code={limit_bps_code} | json={override_bps}).")

    if strict_atr and not meets_target:
        reason_parts = []
        if ask is not None and use_tp is not None and ask > use_tp:
            reason_parts.append(f"ask {ask:.2f} > target {use_tp:.2f}")
        if not regime_up:
            reason_parts.append("regime_up=False")
        reason = " and ".join(reason_parts) if reason_parts else "conditions not met"
        print(f"[HOLD] {stock} strict-atr: {reason}.")
        if not regime_up:
            return

    # --- Direction Change Trigger ---
    try:
        trigger_hit, trigger_reason = detect_direction_change(client, stock)
    except Exception as e:
        print(f"[WARN] {stock} trigger detection failed: {e}")
        trigger_hit, trigger_reason = False, ""

    if trigger_hit and not meets_target:
        print(f"[TRIGGER] {stock} {trigger_reason} detected -> early entry")

        trigger_frac = 0.25
        stage_usd = max(0.0, float(usd or 0.0)) * trigger_frac
        price_cap = ask

        if not _finite(price_cap) or float(price_cap) <= 0.0:
            print(f"[SKIP] {stock} trigger invalid price_cap={price_cap}")
            return

        now_ts = datetime.now(TZ).timestamp()
        if now_ts - _last_trade_ts.get(stock, 0.0) < 1800:
            print(f"[TRIGGER-HOLD] {stock} cooldown active")
            return

        current_mv = (qty_owned * price_cap)
        sym_headroom_mv_trigger = max(0.0, sym_mv_cap - current_mv) if math.isfinite(sym_mv_cap) else float('inf')
        cash_budget_trigger = max(0.0, (cash - CASH_BUFFER))
        headroom_combined_trigger = min(sym_headroom_mv_trigger, cash_budget_trigger)

        desired_shares = int(stage_usd / price_cap) if price_cap > 0 else 0

        buy_shares, used_budget, size_note = partial_size(
            symbol=stock,
            price=price_cap,
            desired_shares=desired_shares,
            total_equity_usd=equity,
            current_mv_usd=current_mv,
            headroom_usd=headroom_combined_trigger,
            usd_per_symbol_cap=None,
            exp_cap_default=sym_cap_eff,
            log=print
        )
        print(size_note)

        if buy_shares > 0:
            qty = int(buy_shares)

            if min_qty and qty < int(min_qty):
                print(f"[SKIP] {stock} trigger qty={qty} < min_qty={min_qty}")
                return

            est_cost = qty * float(price_cap)
            min_usd_eff_trigger = _load_sym_min_usd(stock, min_usd)
            if min_usd_eff_trigger and est_cost < float(min_usd_eff_trigger):
                print(f"[SKIP] {stock} trigger notional=${est_cost:.2f} < min_usd=${min_usd_eff_trigger:.2f}")
                return

            order = {
                "orderType": "MARKET",
                "session": "NORMAL",
                "duration": "DAY",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [
                    {"instruction": "BUY", "quantity": float(qty),
                     "instrument": {"symbol": stock, "assetType": "EQUITY"}}
                ],
            }

            if not confirm:
                print(f"[PREVIEW-TRIGGER] BUY {qty} {stock} (reason={trigger_reason})")
            else:
                lock_path = str(LOCKS_DIR / f"{stock}.lock")
                with file_lock(lock_path, timeout=10):
                    resp = client.order_place(account_hash, order)

                accepted, status_code, order_id = _broker_order_accepted(resp)
                print(f"[TRIGGER BUY] {stock} qty={qty} reason={trigger_reason} status={status_code} order_id={order_id}")
                if not accepted:
                    print(f"[ORDER-REJECTED] {stock} trigger order not accepted; stage/budget unchanged.")
                    return
                _last_trade_ts[stock] = now_ts

        return

    # ===== BUDGET COMPUTATION =====
    cash_after_reserve = max(0.0, float(cash) - float(CASH_BUFFER))
    if math.isfinite(sym_mv_cap):
        sym_headroom_mv_now = max(0.0, (sym_mv_cap - (qty_owned * _est_price))) if _finite(_est_price) else max(0.0, sym_mv_cap)
    else:
        sym_headroom_mv_now = float("inf")
    sum_headroom = sym_headroom_mv_now
    acct_last4 = _last4(acct_label.split()[0] if acct_label else "")
    daily_remaining = _daily_remaining_usd(active_acct_last4=acct_last4)
    brake_budget = float("inf")
    global_cap_gap = sum_headroom
    min_usd_eff = _load_sym_min_usd(stock, min_usd)

    eff_budget = _log_budget_why(
        print,
        cash_after_reserve=cash_after_reserve,
        sum_headroom=sum_headroom if math.isfinite(sum_headroom) else float("inf"),
        daily_remaining=daily_remaining,
        brake_budget=brake_budget,
        global_cap_gap=global_cap_gap if math.isfinite(global_cap_gap) else float("inf"),
        min_usd=min_usd_eff,
        dd=dd_down,
        brake_on=brake_on
    )

    if eff_budget < float(min_usd_eff or 0.0):
        print(f"[HOLD] budget=${eff_budget:.2f} < min_usd=${min_usd_eff:.2f} (tight caps/headroom; overrides active)")
        print(f"[SKIP] {stock} no viable size (stage_usd={(usd or 0):.2f}, cash={cash:.2f}, sym_cap={sym_cap_eff:.0%}).")
        return

    # --- Stage sizing helper ---
    def compute_stage_frac(idx: int) -> float:
        if N > 1:
            any_frac = any(st.get('frac') is not None for st in stages)
            if any_frac:
                return max(0.0, float(stages[idx].get('frac') or 0.0))
            else:
                return 1.0 / N
        else:
            return 1.0 if (stages[0].get('frac') is None) else max(0.0, float(stages[0]['frac']))

    # --- Order builder per stage ---
    def build_order(price_cap: float) -> dict:
        session = "SEAMLESS" if (hours or "").lower().startswith("ext") else "NORMAL"
        if order_style == "market":
            return {
                "orderType": "MARKET",
                "session": session,
                "duration": "DAY",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [
                    {"instruction": "BUY", "quantity": 0.0, "instrument": {"symbol": stock, "assetType": "EQUITY"}}
                ],
            }
        else:
            return {
                "orderType": "LIMIT",
                "price": float(price_cap),
                "session": session,
                "duration": "DAY",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [
                    {"instruction": "BUY", "quantity": 0.0, "instrument": {"symbol": stock, "assetType": "EQUITY"}}
                ],
            }

    # --- Fire stages ---
    for next_idx in to_fire:
        _, use_thr_val, use_tp = stage_targets[next_idx]
        use_k = ks_used.get(next_idx, k_effective(None))

        if order_style == "market":
            price_cap = ask
        else:
            slip_mult = 1.0 + max(0.0, _f(max_slippage, 0.0))
            proposed = round(ask * slip_mult, 2) if slip_mult > 1.0 else round(ask + 0.01, 2)
            price_cap = min(proposed, round(use_tp, 2)) if strict_atr else proposed

        if (price_cap is None) or (not _finite(price_cap)) or (float(price_cap) <= 0.0):
            print(f"[SKIP] {stock} invalid price_cap={price_cap}")
            continue

        stage_frac = compute_stage_frac(next_idx)
        stage_usd_nominal = max(0.0, float(usd or 0.0)) * stage_frac
        stage_usd = min(stage_usd_nominal, float(eff_budget))
        if stage_usd <= 0.0:
            print(f"[SKIP] {stock} stage budget is zero (usd={usd}, frac={stage_frac:.3f}).")
            continue

        current_mv = (qty_owned * price_cap)
        sym_headroom_mv = max(0.0, sym_mv_cap - current_mv) if math.isfinite(sym_mv_cap) else float('inf')
        cash_budget     = max(0.0, (cash - CASH_BUFFER))
        headroom_combined = min(sym_headroom_mv, cash_budget, eff_budget)

        desired_shares = int(stage_usd / price_cap) if price_cap > 0 else 0

        buy_shares, used_budget, size_note = partial_size(
            symbol=stock,
            price=price_cap,
            desired_shares=desired_shares,
            total_equity_usd=equity,
            current_mv_usd=current_mv,
            headroom_usd=headroom_combined,
            usd_per_symbol_cap=None,
            exp_cap_default=sym_cap_eff,
            log=print
        )
        print(size_note)

        if buy_shares <= 0:
            print(f"[SKIP] {stock} no viable size (stage_usd={stage_usd:.2f}, cash={cash:.2f}, sym_cap={sym_cap_eff:.0%}).")
            continue

        qty = int(buy_shares)
        if min_qty and qty < int(min_qty):
            print(f"[SKIP] {stock} qty={qty} < min_qty={min_qty} (probe-block).")
            continue

        est_cost = qty * float(price_cap)

        if min_usd_eff and est_cost < float(min_usd_eff):
            print(f"[SKIP] {stock} notional=${est_cost:.2f} < min_usd=${min_usd_eff:.2f} (tiny-order-block).")
            continue

        order = build_order(price_cap)
        order["orderLegCollection"][0]["quantity"] = float(qty)

        if not confirm:
            print(f"[PREVIEW] BUY {qty} {stock} @ {order['orderType']} "
                  f"{ (price_cap if order['orderType']=='LIMIT' else '') } "
                  f"(stage {next_idx+1}/{N}; thr={use_thr_val:.2f}% k={use_k:.2f}; ≤ ${stage_usd:.2f}; est ${est_cost:.2f})")
            cash -= est_cost
            qty_owned += qty
            fired.add(next_idx+1)
            st_state['fired'] = sorted(fired)
            st_state['date'] = today_s
            _write_stage_state(stock, st_state)
            _consume_daily(est_cost)
            continue

        lock_path = str(LOCKS_DIR / f"{stock}.lock")
        with file_lock(lock_path, timeout=10):
            resp = client.order_place(account_hash, order)

        accepted, status_code, order_id = _broker_order_accepted(resp)

        print(f"Order submitted. Status={status_code}  OrderID={order_id}")
        if not accepted:
            print(f"[ORDER-REJECTED] {stock} order not accepted; stage/budget unchanged.")
            continue

        fired.add(next_idx+1)
        st_state['fired'] = sorted(fired)
        st_state['date'] = today_s
        _write_stage_state(stock, st_state)

        cash -= est_cost
        qty_owned += qty
        _consume_daily(est_cost)

        if order_id:
            status, fill_px, fill_qty = wait_for_fill(client, account_hash, order_id)
            if status or fill_px:
                log_event(side="BUY", symbol=stock, mode="buy", baseline=baseline_name,
                          threshold_pct=use_thr_val, last=(last or ""), close=close,
                          dip_pct=round(dip_from_base,4), action=(status or "FILLED"),
                          qty=(fill_qty or qty), order_id=order_id,
                          order_status=(status or "FILLED"),
                          fill_price=fill_px, fill_value=((fill_px or 0)*(fill_qty or 0)),
                          notes=f"stage fired {next_idx+1}/{N}; k={use_k:.2f}; gate={gate_mode}")
        _last_trade_ts[stock] = datetime.now(TZ).timestamp()

def run(*args, **kwargs):
    symbol = ""
    if args:
        symbol = str(args[0]).upper()
    elif "symbol" in kwargs:
        symbol = str(kwargs["symbol"]).upper()
    lock_path = str(LOCKS_DIR / "BUYLOW_PORTFOLIO.lock")
    print(f"[LOCK] Waiting for portfolio buy lock ({symbol or 'UNKNOWN'})")
    with file_lock(lock_path, timeout=60):
        print(f"[LOCK] Acquired portfolio buy lock ({symbol or 'UNKNOWN'})")
        return _run_unlocked(*args, **kwargs)

def wait_for_fill(client, account_hash, order_id, timeout_sec=45, interval_sec=3):
    deadline = _time.time() + timeout_sec
    while _time.time() < deadline:
        try:
            det = client.order_details(account_hash, order_id).json()
            status, fill_px, fill_qty = extract_fill(det)
            status = (status or "").upper()
            if status in ("FILLED","REJECTED","CANCELED","EXPIRED"):
                return status, fill_px, fill_qty
            if fill_px and fill_qty:
                return "FILLED", fill_px, fill_qty
        except Exception:
            pass
        _time.sleep(interval_sec)
    return "", None, 0.0

# --- helpers for CLI parsing ---
def _parse_spread_limits(s: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in (s or "").split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip().upper()] = int(float(v.strip()))
            except Exception:
                pass
    return out

def _parse_float_map(s: str) -> dict[str, float]:
    out: dict[str, float] = {}
    if not s:
        return out
    for part in s.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            out[k.strip().upper()] = float(v.strip())
        except Exception:
            continue
    return out

# ---------- CLI ----------
def parse_args():
    ap = argparse.ArgumentParser(description="Buy-Low (staged-capable, looping, multi-symbol) with caps, brakes, and ATR ladder.")
    ap.add_argument("--symbols", nargs='+', help="One or more tickers (space-separated)")
    ap.add_argument("--usd-per-symbol", type=float, help="Budget (USD) per symbol (uniform)")
    ap.add_argument("--usd-map", help="Per-symbol USD map, e.g. SPY=600,QQQ=450")
    ap.add_argument("--order-style", choices=["limit","market"], default="limit")
    ap.add_argument("--max-slippage", type=float, default=0.003, help="Fractional headroom (0.003=0.3%)")
    ap.add_argument("--tz", default="America/Detroit")
    ap.add_argument("--hours", choices=["regular","extended"], default="regular")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("symbol_positional", nargs="?", help="Ticker (legacy single)")
    ap.add_argument("--usd", type=float, help="USD budget (legacy)")
    ap.add_argument("--confirm", action="store_true", help="Place real orders")
    ap.add_argument("--atr-k", type=float, default=1.5, help="Uniform ATR-K if no per-symbol override")
    ap.add_argument("--atr-k-map", help="Per-symbol ATR-K map, e.g. SPY=1.4,NVDA=1.1")
    ap.add_argument("--dd-brake", type=float, default=10.0)
    ap.add_argument("--exp-cap", type=float, default=0.60)
    ap.add_argument("--sym-cap", type=float, default=0.05)
    ap.add_argument("--regime", default=DEFAULT_REGIME_SYMBOL)
    ap.add_argument("--soft-brake", type=float)
    ap.add_argument("--hard-brake", type=float)
    ap.add_argument("--brake-verbose", action="store_true")
    ap.add_argument("--buy-dic", default=BUY_DIC_PATH)
    ap.add_argument("--reset-stages", action="store_true")
    ap.add_argument("--stage-expire-days", type=int, default=3)
    ap.add_argument("--batch-stages", action="store_true",
                    help="If price meets multiple stage targets, fire all unfired stages this pass.")
    ap.add_argument("--strict-atr", action="store_true")
    ap.add_argument("--no-spread-override", action="store_true")
    ap.add_argument("--min-qty", type=int, default=1)
    ap.add_argument("--min-usd", type=float, default=0.0)
    ap.add_argument("--gate-mode", choices=["max","thr","atr"], default="max",
                    help="How to compute dip: max(thr, ATR*K) | thr | atr")
    ap.add_argument("--dip-baseline", choices=["prevclose","todayhigh"], default="prevclose",
                    help="Price to measure dip from")
    ap.add_argument("--acct", dest="args_acct", default="")
    ap.add_argument("--acct-file", dest="args_acct_file", default=str(CONFIG_DIR / "acct.json"))
    ap.add_argument("--loop", action="store_true", help="Run forever while honoring interval/cooldown and window")
    ap.add_argument("--interval-sec", type=int, default=60, help="Seconds between trading cycles INSIDE the trading window")
    ap.add_argument("--cooldown-sec", type=int, default=600, help="Seconds to sleep when OUTSIDE the trading window")
    ap.add_argument("--spread-limits", help="Comma list like DEFAULT=10,NVDA=8,QQQ=6,SPY=5 (bps)")
    ap.add_argument("--log-dir", default="", help="Optional log directory (rotating daily file)")
    ap.add_argument("--atrk-file", default=ATRK_OVERRIDES_FILE,
                    help="Per-symbol ATR-K overrides JSON (hot-reloaded each pass). Keys: SYMBOL or DEFAULT")
    return ap.parse_args()

# ---------- Main ----------
def main():
    a = parse_args()

    symbols: List[str] = []
    if a.symbols:
        symbols.extend([s.strip().upper() for s in a.symbols if s.strip()])
    if a.symbol_positional:
        symbols.append(a.symbol_positional.strip().upper())
    symbols = [s for s in symbols if s]
    if not symbols:
        print("[ERR] --symbols SYMBOL [SYMBOL ...] is required")
        return

    usd_map = _parse_float_map(getattr(a, 'usd_map', None))

    global SPREAD_LIMIT_BPS
    if getattr(a, "spread_limits", None):
        SPREAD_LIMIT_BPS.update(_parse_spread_limits(a.spread_limits))

    if getattr(a, 'log_dir', ''):
        try:
            os.makedirs(a.log_dir, exist_ok=True)
            sys.stdout = RotatingTee(sys.stdout, a.log_dir, base_name='buylow')
            sys.stderr = RotatingTee(sys.stderr, a.log_dir, base_name='buylow_err')
            print(f"[INFO] Logging to {a.log_dir}\\buylow_YYYYMMDD.log")
        except Exception as e:
            print(f"[WARN] Rotating logger not active: {e}")

    def one_pass():
        print(f"[PASS] {datetime.now(TZ).strftime('%H:%M:%S')} — starting auto-gate evaluation for {len(symbols)} symbols")

        app_key = os.getenv("app_key"); app_secret = os.getenv("app_secret")
        if not app_key or not app_secret:
            print("[ERR] Set env vars app_key/app_secret before running.")
            return
        try:
            client_hist = schwabdev.Client(app_key, app_secret, CALLBACK_URL, TOKENS_FILE)
            with file_lock(TOKENS_FILE + ".lock"):
                client_hist.update_tokens()
        except Exception as e:
            print(f"[ERROR] token refresh (history client) failed: {e}")
            return

        atrk_file_map = load_atrk_overrides(getattr(a, "atrk_file", ATRK_OVERRIDES_FILE))

        def atr_series_rollmean(candles, n=ATR_LEN):
            if not candles or len(candles) < n + 21:
                return []
            TR = []
            prev_close = candles[0]["close"]
            for c in candles[1:]:
                hi_lo = c["high"] - c["low"]
                hi_cl = abs(c["high"] - prev_close)
                lo_cl = abs(c["low"] - prev_close)
                TR.append(max(hi_lo, hi_cl, lo_cl))
                prev_close = c["close"]
            atrs = []
            window_sum = sum(TR[:n])
            atrs.append(window_sum / n)
            for i in range(n, len(TR)):
                window_sum += TR[i] - TR[i - n]
                atrs.append(window_sum / n)
            return atrs

        for sym in symbols:
            try:
                usd_eff = usd_map.get(sym, None)
                if usd_eff is None:
                    usd_eff = (a.usd_per_symbol if a.usd_per_symbol is not None else (a.usd or 0.0))

                gate_mode_auto = (a.gate_mode or "max")

                candles = get_daily_history(client_hist, sym)
                daily_atr_val = None
                if candles and len(candles) >= (ATR_LEN + 30):
                    atrs = atr_series_rollmean(candles, n=ATR_LEN)
                    daily_atr_val = atrs[-1] if atrs else None

                intraday_atr_val = None
                try:
                    data_intra = client_hist.price_history(
                        sym, period_type="day", period=1, frequency_type="minute", frequency=5
                    ).json()
                    bars = (data_intra or {}).get("candles") or []
                    if bars and len(bars) > 50:
                        trs = []
                        prev_close = bars[0]["close"]
                        for c in bars[1:]:
                            hi_lo = c["high"] - c["low"]
                            hi_cl = abs(c["high"] - prev_close)
                            lo_cl = abs(c["low"] - prev_close)
                            trs.append(max(hi_lo, hi_cl, lo_cl))
                            prev_close = c["close"]
                        intraday_atr_val = sum(trs[-50:]) / 50.0
                except Exception as e:
                    if a.verbose:
                        print(f"[AUTO-GATE] {sym}: intraday ATR unavailable ({e})")

                if _finite(daily_atr_val) and _finite(intraday_atr_val) and float(daily_atr_val) > 0:
                    ratio = float(intraday_atr_val) / float(daily_atr_val)
                    if ratio >= 1.30:
                        gate_mode_auto = "atr"
                        strict_flag = True
                    elif ratio >= 1.10:
                        gate_mode_auto = "atr"
                        strict_flag = a.strict_atr
                    else:
                        gate_mode_auto = "max"
                        strict_flag = a.strict_atr
                    if a.verbose:
                        print(f"[AUTO-GATE] {sym}: intraday/daily ATR ratio={ratio:.2f} → gate_mode={gate_mode_auto} strict={strict_flag}")
                else:
                    strict_flag = a.strict_atr
                    if a.verbose:
                        print(f"[AUTO-GATE] {sym}: no intraday/daily ATR; fallback gate={gate_mode_auto} strict={strict_flag}")

                run(sym, usd_eff, a.confirm,
                    atr_k=a.atr_k,
                    dd_brake_pct=a.dd_brake,
                    exp_cap=a.exp_cap,
                    sym_cap=a.sym_cap,
                    regime_symbol=a.regime,
                    order_style=a.order_style,
                    max_slippage=a.max_slippage,
                    hours=a.hours,
                    soft_brake=a.soft_brake,
                    hard_brake=a.hard_brake,
                    brake_verbose=a.brake_verbose,
                    strict_atr=strict_flag,
                    no_spread_override=a.no_spread_override,
                    min_qty=a.min_qty,
                    min_usd=a.min_usd,
                    args_acct=a.args_acct,
                    args_acct_file=a.args_acct_file,
                    buy_dic=a.buy_dic,
                    reset_stages=a.reset_stages,
                    stage_expire_days=a.stage_expire_days,
                    batch_stages=a.batch_stages,
                    gate_mode=gate_mode_auto,
                    dip_baseline=a.dip_baseline,
                    atrk_file_map=atrk_file_map,
                    atrk_map_cli=_parse_float_map(getattr(a, 'atr_k_map', None)),
                    atrk_uniform_cli=a.atr_k
                )

            except Exception as e:
                print(f"[WARN] {sym}: pass failed: {e}")
            _time.sleep(0.5)

    if a.loop:
        last_run_ts = 0.0
        interval = max(1, int(getattr(a, 'interval_sec', 60) or 60))
        cooldown = max(1, int(getattr(a, 'cooldown_sec', 600) or 600))
        while True:
            now = datetime.now(TZ)
            if in_window(now, a.hours):
                now_ts = _time.time()
                elapsed = now_ts - last_run_ts
                if elapsed >= interval:
                    one_pass()
                    last_run_ts = now_ts
                else:
                    _time.sleep(min(1, max(0, interval - elapsed)))
            else:
                print("[INFO] Outside trading window; sleeping.")
                _time.sleep(cooldown)
    else:
        one_pass()

if __name__ == "__main__":
    main()
