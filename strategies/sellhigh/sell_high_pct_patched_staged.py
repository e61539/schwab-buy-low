#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# VERSION_BANNER: 2026-01-21 FINAL4 (SELL_LIMIT_FIX + REJECT_DETAIL_DUMP)
# sell_high_pct_patched_staged.py (CLEAN, dust-proof)
#
# - Account nicknames via C:\temp\schwab_accounts.json  (env DEFAULT_ACCT)
# - Hot-reload of C:\temp\sell.dic (no restart)
# - Staged TPs from number|object|list
# - Spread guard, cooldowns, market-hours gate, pending watcher
# - WHY/CHK/SKIP/SEND diagnostics
# - === DUST-PROOF: last stage always sells remainder (no residue shares)
#
# OPTIONAL:
# - Volatility-regime SellHigh adjustment via ATR% from Schwab pricehistory:
#   Enable:  --vol-sell on
#   Disable: --vol-sell off   (default)

import os, sys, json, time, math
from pathlib import Path
from datetime import datetime, time as _time, timedelta
from typing import Dict, List

# -------- Timezone helpers --------
try:
    from zoneinfo import ZoneInfo  # Py3.9+
except Exception:
    ZoneInfo = None


def _safe_zoneinfo(tzname: str):
    if not ZoneInfo:
        return None
    try:
        return ZoneInfo(tzname)
    except Exception as e:
        print(f"[WARN] timezone {tzname} unavailable; using local system time: {e}", flush=True)
        return None

# -------- ET timestamp helpers (to match Schwab UI) --------
_ET_TZNAME = os.getenv("DISPLAY_TZ", "America/New_York")
_ET_TZ = _safe_zoneinfo(_ET_TZNAME)

def _now_et() -> datetime:
    return datetime.now(_ET_TZ) if _ET_TZ else datetime.utcnow()


def _tz(tzname: str):
    return _safe_zoneinfo(tzname)

def _is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5

def _seconds_until_next_open(now_local: datetime, start_t: _time) -> int:
    if _is_weekday(now_local) and now_local.time() < start_t:
        target = datetime.combine(now_local.date(), start_t, now_local.tzinfo)
    else:
        d = 1
        while True:
            cand = now_local + timedelta(days=d)
            if _is_weekday(cand):
                target = datetime.combine(cand.date(), start_t, now_local.tzinfo)
                break
            d += 1
    return max(1, int((target - now_local).total_seconds()))

# -------- defaults / env --------
# Phase 2 path hardening: resolve paths from the repo root so this strategy can
# be launched from Task Scheduler, VSCode, or any shell current directory.
ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parents[2])).resolve()
CONFIG_DIR = ROOT / "config"
RUNTIME_DIR = ROOT / "runtime"
LOCKS_DIR = RUNTIME_DIR / "locks"
STATE_DIR = RUNTIME_DIR / "state"
for _import_dir in (Path(__file__).resolve().parent, ROOT):
    if str(_import_dir) not in sys.path:
        sys.path.insert(0, str(_import_dir))

CALLBACK_URL = "https://127.0.0.1"
TOKENS_FILE  = os.getenv("TOKENS_FILE", str(CONFIG_DIR / "tokens.txt"))
SELL_DIC     = os.getenv("SELL_DIC",   str(Path(__file__).resolve().parent / "sell.dic"))

DEFAULT_COOLDOWN_SEC = int(os.getenv("SELL_COOLDOWN_SEC", "600") or 600)
DEFAULT_INTERVAL_SEC = int(os.getenv("SELL_INTERVAL_SEC", "60") or 60)
CROSS_COOLDOWN_SEC   = int(os.getenv("CROSS_COOLDOWN_SEC", "120") or 120)
SPREAD_MAX_PCT       = float(os.getenv("SPREAD_MAX_PCT", "1.0") or 1.0)

def _default_accounts_map() -> str:
    repo_map = CONFIG_DIR / "schwab_accounts.json"
    if repo_map.exists():
        return str(repo_map)
    return r"C:\temp\schwab_accounts.json"


ACCOUNTS_MAP = os.getenv("SCHWAB_ACCOUNTS_FILE", _default_accounts_map())
DEFAULT_ACCOUNT_NICK = os.getenv("DEFAULT_ACCT", "IRA1")

# ------------------- formatting / heartbeat -------------------
def _fmt(x, nd=2):
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return "-"

def heartbeat_sell(sym, *, last=None, bid=None, ask=None, avg=None, tp=None,
                   spread_pct_val=None, pos_qty=None, cooldown_ok_flag=None, armed=None,
                   pending_sell_qty: int = 0, stage_text: str = "", extra: str = ""):
    ts_dt = _now_et()
    ts = ts_dt.strftime("%Y-%m-%d %H:%M:%S") + (f" {_ET_TZNAME}" if _ET_TZ else " UTC")
    msg = (f"[HB] {ts} {sym} last={_fmt(last)} bid/ask={_fmt(bid)}/{_fmt(ask)} "
           f"avg={_fmt(avg)} tp={_fmt(tp)} spr%={_fmt(spread_pct_val)} "
           f"qty={pos_qty if pos_qty is not None else '-'}"
           + (f" pending_sell={pending_sell_qty}" if pending_sell_qty else "")
           + (f" stage={stage_text}" if stage_text else "")
           + (f" {extra}" if extra else "")
           + f" cd_ok={cooldown_ok_flag} armed={armed}")
    print(msg, flush=True)

# ------------------- cooldown helpers -------------------
def _cd_file(sym: str) -> Path:
    return LOCKS_DIR / f"COOLDOWN_{sym.upper()}.ts"

def cooldown_ok(sym: str) -> bool:
    p = _cd_file(sym)
    if not p.exists():
        return True
    try:
        return (time.time() - p.stat().st_mtime) >= CROSS_COOLDOWN_SEC
    except Exception:
        return True

def mark_cooldown(sym: str):
    p = _cd_file(sym)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass

# ------------------- trade logger (CSV fallback) -------------------
def _csv_fallback_logger(**kw):
    try:
        log_path = Path(os.getenv("TRADE_LOG_CSV", r"C:\temp\trades_log.csv"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fields = ["ts","strategy","side","symbol","mode","baseline","threshold_pct","last","close",
                  "action","qty","order_id","order_status","fill_price","fill_value","notes"]
        row = {k: kw.get(k) for k in fields}
        row["ts"] = row.get("ts") or datetime.now().isoformat(timespec="seconds")
        write_header = not log_path.exists()
        import csv
        with log_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if write_header:
                w.writeheader()
            w.writerow(row)
    except Exception:
        pass

try:
    from trade_logger import log_event as _log_event
    def log_event(**kw):
        try:
            if "ts" not in kw:
                kw["ts"] = datetime.now().isoformat(timespec="seconds")
            _log_event(**kw)
        except Exception as e:
            print(f"[WARN] trade_logger.log_event failed: {e}", flush=True)
            _csv_fallback_logger(**kw)
except Exception as e:
    print(f"[WARN] trade_logger import failed: {e}", flush=True)
    def log_event(**kw):
        _csv_fallback_logger(**kw)

# ------------------- Schwab config / creds -------------------
def load_cfg(path=r"C:\temp\schwab_config.json") -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return json.loads(p.read_text(encoding="utf-8"))

def resolve_creds() -> dict:
    cfg = load_cfg()
    api_key    = os.getenv("app_key")    or cfg.get("api_key")
    app_secret = os.getenv("app_secret") or cfg.get("app_secret")
    if not api_key or not app_secret:
        print("[ERROR] Set env app_key/app_secret or put api_key/app_secret in C:\\temp\\schwab_config.json", flush=True)
        sys.exit(2)
    return {
        "api_key": api_key,
        "app_secret": app_secret,
        "callback_url": cfg.get("callback_url", CALLBACK_URL),
        "tokens_file": cfg.get("tokens_file", TOKENS_FILE),
    }

def make_client(creds: dict):
    import schwabdev, msvcrt
    from contextlib import contextmanager

    @contextmanager
    def token_lock(path, timeout=15):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path + ".lock", "a+")
        start = time.time()
        locked = False
        try:
            while time.time() - start < timeout:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.2)
            yield
        finally:
            try:
                if locked:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            fh.close()

    cl = schwabdev.Client(creds["api_key"], creds["app_secret"], creds["callback_url"], creds["tokens_file"])
    with token_lock(creds["tokens_file"], timeout=15):
        try:
            cl.update_tokens()
        except Exception:
            pass
    cl.__token_lock__ = token_lock
    cl.__creds__ = creds
    return cl

# ------------------- HTTP helpers with refresh -------------------
class _Unauthorized(Exception):
    pass

def _do_request(method: str, url: str, client, **kwargs):
    import requests
    headers = kwargs.setdefault("headers", {})
    headers.setdefault("Accept", "application/json")
    headers["Authorization"] = f"Bearer {client.access_token}"
    r = requests.request(method, url, timeout=(10, 30), **kwargs)
    if r.status_code == 401:
        raise _Unauthorized()
    r.raise_for_status()
    return r

def _refresh_tokens(client):
    token_lock = getattr(client, "__token_lock__", None)
    creds = getattr(client, "__creds__", None)
    try:
        if not token_lock or not creds:
            client.update_tokens()
            return True
        with token_lock(creds["tokens_file"], timeout=15):
            client.update_tokens()
        print("[AUTH] Access token refreshed.", flush=True)
        return True
    except Exception as e:
        print("[AUTH] Refresh failed:", e, flush=True)
        return False

def _with_refresh(method: str, url: str, client, *, max_retries=2, **kwargs):
    backoff = 2.0
    import requests
    for _ in range(max_retries):
        try:
            return _do_request(method, url, client, **kwargs)
        except _Unauthorized:
            if not _refresh_tokens(client):
                time.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            return _do_request(method, url, client, **kwargs)
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (429,) or (code and 500 <= code < 600):
                time.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            raise
        except requests.RequestException:
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)
            continue
    raise RuntimeError("request failed after retries")

# ------------------- Schwab helpers -------------------
def parse_json(x):
    try:
        return x.json() if hasattr(x, "json") else (json.loads(x) if isinstance(x, (str, bytes, bytearray)) else x)
    except Exception:
        return x

# ----- Housekeeping -----
def preflight_lock_cleanup(sym: str, pos_qty: int, lock_dir: str = r"C:\temp\locks") -> None:
    try:
        if pos_qty != 0:
            return
        targets = [
            os.path.join(lock_dir, f"STAGES_{sym}.json"),
            os.path.join(lock_dir, f"COOLDOWN_SELL_{sym}.json"),
            os.path.join(lock_dir, f"PENDING_SELL_{sym}.json"),
        ]
        for p in targets:
            try:
                if os.path.exists(p):
                    os.remove(p)
                    print(f"[CLEAN] removed stale {os.path.basename(p)} (qty=0)")
            except Exception as e:
                print(f"[WARN] lock cleanup failed: {p}: {e}")
    except Exception as e:
        print(f"[WARN] preflight_lock_cleanup error: {e}")

def list_positions(account_hash: str, client) -> Dict[str, Dict[str, float]]:
    url = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}"
    r = _with_refresh("GET", url, client, params={"fields": "positions"})
    data = r.json()
    sa = data.get("securitiesAccount", {})
    pos = sa.get("positions", []) or []
    out = {}
    for p in pos:
        instr = p.get("instrument", {}) or {}
        sym = (instr.get("symbol") or "").upper()
        if not sym:
            continue
        qty = float(p.get("longQuantity") or p.get("quantity") or 0.0)
        preflight_lock_cleanup(sym, qty)
        avg = p.get("averagePrice")
        try:
            avg = float(avg) if avg is not None else None
        except Exception:
            avg = None
        out[sym] = {"qty": qty, "avg": avg}
    return out

def get_quote(client, sym: str):
    url = "https://api.schwabapi.com/marketdata/v1/quotes"
    r = _with_refresh("GET", url, client, params={"symbols": sym})
    node = (r.json().get(sym) or {}).get("quote", {})
    out = {
        "bid": node.get("bidPrice") or node.get("bid"),
        "ask": node.get("askPrice") or node.get("ask"),
        "last": node.get("lastPrice") or node.get("closePrice") or node.get("mark"),
        "close": node.get("closePrice") or node.get("lastPrice"),
    }
    for k, v in list(out.items()):
        out[k] = float(v) if v is not None else None
    return out

# ---- spread guard helper (you asked about this) ----
def spread_pct(bid, ask):
    if not bid or not ask:
        return 1e9
    mid = 0.5 * (bid + ask)
    if not mid or mid <= 0:
        return 1e9
    return (ask - bid) / mid * 100.0

# ===== ATR% / Volatility helpers (OPTIONAL) =====
# Enable:  --vol-sell on
# Disable: --vol-sell off (default)
_ATR_CACHE = {}  # sym -> {"ts": epoch, "atr": float, "close": float}

def _atr_pct(client, sym: str, lookback: int = 14):
    """
    Compute ATR% from Schwab daily candles (pricehistory).
    Returns ATR% (e.g. 1.8 means ~1.8% daily ATR).
    Cached for 5 minutes per symbol.
    """
    now = time.time()
    cache = _ATR_CACHE.get(sym)
    if cache and (now - cache["ts"]) < 300:
        c = cache.get("close") or 0.0
        return (cache.get("atr") / c * 100.0) if c else None

    try:
        url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
        params = {
            "symbol": sym,
            "periodType": "day",
            "period": int(lookback) + 3,
            "frequencyType": "daily",
            "frequency": 1,
            "needExtendedHoursData": "false",
        }
        r = _with_refresh("GET", url, client, params=params)
        candles = (r.json() or {}).get("candles") or []
        if len(candles) < (lookback + 1):
            return None

        trs = []
        for i in range(1, len(candles)):
            c = candles[i]
            p = candles[i - 1]
            hi = float(c.get("high"))
            lo = float(c.get("low"))
            pc = float(p.get("close"))
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))

        atr = sum(trs[-lookback:]) / float(lookback)
        close = float(candles[-1].get("close"))
        _ATR_CACHE[sym] = {"ts": now, "atr": atr, "close": close}
        return (atr / close * 100.0) if close else None
    except Exception:
        return None

def _vol_regime(atr_pct: float | None):
    if atr_pct is None:
        return "unknown"
    if atr_pct < 1.2:
        return "low"
    if atr_pct < 2.5:
        return "normal"
    return "high"

def _dyn_sell_adjust(stage_thr_pct: float, atr_pct: float | None):
    """
    Dynamic SellHigh threshold adjustment:
    - high vol: take profit sooner (lower thr)
    - low vol: let it run a bit more (higher thr)
    """
    reg = _vol_regime(atr_pct)
    if reg == "high":
        return max(0.5 * stage_thr_pct, stage_thr_pct - 2.0), reg
    if reg == "low":
        return stage_thr_pct + 1.0, reg
    if reg == "normal":
        return stage_thr_pct, reg
    return stage_thr_pct, reg

# ------------------- Orders -------------------
def _session_for_hours(hours: str) -> str:
    # Schwab uses SEAMLESS for “day + ext” (regular + extended).
    # Keep NORMAL for regular hours.
    return "SEAMLESS" if (hours or "").lower() == "extended" else "NORMAL"

def place_limit_sell(client, account_hash: str, sym: str, qty: int, limit_px: float, *, session: str = "NORMAL"):
    """
    Places a LIMIT sell.

    Surgical fixes:
      - quantity MUST be int for Schwab (not float)
      - session selectable (NORMAL vs SEAMLESS)
      - capture Location and also response body for debugging
    """
    order = {
        "orderType": "LIMIT",
        "session": session,
        "price": float(limit_px),
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "SELL",
                "quantity": int(qty),  # <<< FIX #1: INT quantity
                "instrument": {"symbol": sym, "assetType": "EQUITY"},
            }
        ],
    }
    url = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders"
    r = _with_refresh("POST", url, client, json=order, headers={"Content-Type": "application/json"})

    try:
        oid = r.headers.get("Location", "") or ""
    except Exception:
        oid = ""

    # Best-effort response body (Schwab sometimes includes details here)
    resp_body = None
    try:
        resp_body = r.json()
    except Exception:
        try:
            resp_body = (r.text or "").strip()
        except Exception:
            resp_body = None

    return {"status": getattr(r, "status_code", "OK"), "order_id": oid, "resp_body": resp_body}

def fetch_order(client, account_hash: str, order_id: str):
    """
    order_id can be either:
      * a bare order ID, or
      * the full Location URL returned from place_limit_sell().
    """
    if not order_id:
        return None
    if order_id.startswith("http://") or order_id.startswith("https://"):
        url = order_id
    else:
        url = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders/{order_id}"
    try:
        r = _with_refresh("GET", url, client)
        return r.json()
    except Exception as e:
        print(f"[WARN] fetch_order failed for {order_id}: {e}", flush=True)
        return None

def filled_qty(order) -> float | None:
    if not order:
        return None
    total = 0.0

    legs = order.get("orderLegCollection") or []
    for leg in legs:
        fq = leg.get("filledQuantity")
        if fq is None:
            fq = leg.get("executedQuantity")
        if fq is not None:
            try:
                total += float(fq)
            except Exception:
                pass

    if total <= 0:
        for k in ("filledQuantity", "executedQuantity"):
            v = order.get(k)
            if v is not None:
                try:
                    total = float(v)
                    break
                except Exception:
                    pass

    if total <= 0:
        children = order.get("childOrderStrategies") or []
        for ch in children:
            fq = filled_qty(ch)
            if fq:
                total += fq

    return total if total > 0 else None

def order_filled(order) -> bool:
    if not order:
        return False
    status = (order.get("status") or "").upper()
    if status in {"FILLED", "EXECUTED", "COMPLETED"}:
        return True
    if status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
        return False

    try:
        fq = filled_qty(order) or 0.0
        req_total = 0.0
        legs = order.get("orderLegCollection") or []
        for leg in legs:
            q = leg.get("quantity")
            if q is not None:
                req_total += float(q)
        if req_total > 0 and fq >= req_total:
            return True
    except Exception:
        pass
    return False

# ------------------- Stage state -------------------
def _stage_state_file(sym: str) -> Path:
    return STATE_DIR / f"STAGES_{sym.upper()}.json"

def _load_stage_state(sym: str) -> dict:
    p = _stage_state_file(sym)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"fired_idxs": []}

def _mark_stage_fired(sym: str, idx: int):
    p = _stage_state_file(sym)
    p.parent.mkdir(parents=True, exist_ok=True)
    st = _load_stage_state(sym)
    if idx not in st.get("fired_idxs", []):
        st.setdefault("fired_idxs", []).append(idx)
    try:
        p.write_text(json.dumps(st, indent=0), encoding="utf-8")
    except Exception:
        pass

def _reset_stage_state(sym: str):
    p = _stage_state_file(sym)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        try:
            p.write_text(json.dumps({"fired_idxs": []}), encoding="utf-8")
        except Exception:
            pass

# ------------------- sell.dic helpers -------------------
def _as_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def _normalize_one(obj):
    if isinstance(obj, (int, float)):
        return {"thr_pct": float(obj), "frac": None, "cap_pct": None, "cap_abs": None, "cooldown_sec": None}
    if isinstance(obj, dict):
        thr  = obj.get("thr") or obj.get("thr_pct") or obj.get("threshold_pct")
        frac = obj.get("frac") or obj.get("qty_pct")
        return {
            "thr_pct": _as_float(thr, None),
            "frac": _as_float(frac, None),
            "cap_pct": _as_float(obj.get("cap_pct"), None),
            "cap_abs": _as_float(obj.get("cap_abs"), None),
            "cooldown_sec": int(_as_float(obj.get("cooldown_sec"), 0) or 0) or None,
        }
    return None

def _normalize_value(v):
    if isinstance(v, list):
        out = []
        for it in v:
            st = _normalize_one(it)
            if st and st["thr_pct"] is not None:
                out.append(st)
        out.sort(key=lambda s: s["thr_pct"])
        return out
    st = _normalize_one(v)
    return [st] if (st and st["thr_pct"] is not None) else []

def _load_sell_dic_raw(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def _build_rules(raw: dict):
    d_raw = {(k.upper() if isinstance(k, str) else k): v for k, v in raw.items()}
    default_stages = _normalize_value(d_raw.get("DEFAULT", 6.0))
    return d_raw, default_stages

# ------------------- CLI helpers / acct map -------------------
def _arg_value(flag: str, default: str = None):
    if flag in sys.argv:
        try:
            return sys.argv[sys.argv.index(flag) + 1]
        except Exception:
            return default
    return default

def _read_json_utf8_or_sig(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}

def _load_acct_map(path=ACCOUNTS_MAP):
    m = _read_json_utf8_or_sig(path) or {}
    out = {}
    for k, v in m.items():
        try:
            if isinstance(v, dict) and v.get("accountNumber"):
                num = str(v["accountNumber"]).strip()
            else:
                num = str(v).strip()
            if not num:
                continue
            out[str(k).upper()] = num
            out[num] = num
            if len(num) >= 4:
                out[num[-4:]] = num
        except Exception:
            pass
    return out

def get_account_hash_for(client, acct_arg: str | None):
    linked = parse_json(client.account_linked())
    accounts = linked if isinstance(linked, list) else [linked]
    if not accounts:
        raise RuntimeError("No linked accounts returned by Schwab")

    want = (acct_arg or DEFAULT_ACCOUNT_NICK or "").strip()
    amap = _load_acct_map()
    want_num = amap.get(want.upper()) or amap.get(want) or (amap.get(want[-4:]) if len(want) >= 4 else None)
    if not want_num and want.isdigit():
        want_num = want

    if want_num:
        for a in accounts:
            if str(a.get("accountNumber")) == str(want_num):
                return a["hashValue"], str(want_num)
        raise RuntimeError(
            f"Requested account {want} (→ {want_num}) not in linked: {[a.get('accountNumber') for a in accounts]}"
        )

    if len(accounts) == 1:
        return accounts[0]["hashValue"], str(accounts[0]["accountNumber"])

    raise RuntimeError(
        f"Nickname '{want or '<none>'}' not found in {ACCOUNTS_MAP}. "
        f"Add it or pass --acct <number>. Linked: {[a.get('accountNumber') for a in accounts]}"
    )

# ================= MAIN =================
def main():
    if len(sys.argv) < 2:
        print(
            r"Usage: python sell_high_pct_patched_staged.py <SYMBOL|ALL> [--sell-dic C:\temp\sell.dic] "
            r"[--min-shares 1] [--cooldown 600] [--interval 60] [--sell-frac 1.0] "
            r"[--vol-sell on|off] [--confirm] [--verbose] "
            r"[--hours regular|extended] [--on-close sleep|exit] [--tz America/New_York] [--acct IRA1|IRA2|<number>]"
        )
        sys.exit(1)

    # sanitize argv (so stray flags don’t crash symbol parsing)
    def _sanitize_argv(argv):
        needs_val = {
            "--sell-dic","--min-shares","--cooldown","--interval","--sell-frac",
            "--hours","--on-close","--tz","--acct","--vol-sell"
        }
        no_val = {"--confirm","--verbose","--dryrun"}
        cleaned = [argv[0]]
        i = 1
        while i < len(argv):
            tok = argv[i]
            if tok in no_val:
                cleaned.append(tok)
                i += 1
                continue
            if tok in needs_val:
                cleaned.append(tok)
                if i + 1 < len(argv):
                    cleaned.append(argv[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            cleaned.append(tok)
            i += 1
        return cleaned

    sys.argv = _sanitize_argv(sys.argv)

    def _extract_symbols(argv):
        needs_val = {
            "--sell-dic","--min-shares","--cooldown","--interval","--sell-frac",
            "--hours","--on-close","--tz","--acct","--vol-sell"
        }
        no_val = {"--confirm","--verbose","--dryrun"}
        out = []
        i = 1
        while i < len(argv):
            tok = argv[i]
            if tok in no_val:
                i += 1
                continue
            if tok in needs_val:
                i += 2
                continue
            if tok.startswith("--"):
                i += 1
                continue
            out.append(tok.upper())
            i += 1
        return out

    symbols_cli = _extract_symbols(sys.argv)
    confirm = ("--confirm" in sys.argv) and ("--dryrun" not in sys.argv)
    verbose = ("--verbose" in sys.argv)

    def why(msg: str):
        if verbose:
            print(msg, flush=True)

    sell_dic_path = _arg_value("--sell-dic", SELL_DIC) or SELL_DIC
    min_shares    = int(float(_arg_value("--min-shares", "1") or 1))
    cooldown_sec  = int(float(_arg_value("--cooldown", str(DEFAULT_COOLDOWN_SEC)) or DEFAULT_COOLDOWN_SEC))
    interval_sec  = int(float(_arg_value("--interval", str(DEFAULT_INTERVAL_SEC)) or DEFAULT_INTERVAL_SEC))
    sell_frac     = max(0.01, min(1.0, float(_arg_value("--sell-frac", "1.0") or 1.0)))

    vol_sell = (_arg_value("--vol-sell", "off") or "off").lower()  # on|off
    if vol_sell not in {"on", "off"}:
        vol_sell = "off"

    hours    = (_arg_value("--hours", "regular") or "regular").lower()
    on_close = (_arg_value("--on-close", "sleep") or "sleep").lower()
    tz_name  = _arg_value("--tz", os.getenv("MARKET_TZ", "America/New_York")) or "America/New_York"
    if hours not in {"regular","extended"}:
        hours = "regular"
    if on_close not in {"sleep","exit"}:
        on_close = "sleep"

    creds = resolve_creds()
    client = make_client(creds)

    acct_arg = _arg_value("--acct") or DEFAULT_ACCOUNT_NICK
    account_hash, acct_num = get_account_hash_for(client, acct_arg)
    print(f"[ACCT] Using {acct_arg} -> {acct_num}", flush=True)

    # hot-reload rules
    def _reload_rules():
        try:
            raw = _load_sell_dic_raw(sell_dic_path)
        except Exception as e:
            print(f"[WARN] could not read {sell_dic_path}; using DEFAULT=6.0; err={e}", flush=True)
            return {"DEFAULT": 6.0}, _normalize_value(6.0)
        d_raw, default_stages = _build_rules(raw)
        if verbose:
            print(f"[SELL.DIC] Loaded keys: {', '.join(sorted([str(k) for k in d_raw.keys()]))}", flush=True)
        return d_raw, default_stages

    try:
        last_mtime = Path(sell_dic_path).stat().st_mtime
    except Exception:
        last_mtime = -1
    d_raw, default_stages = _reload_rules()

    def _stages_for_symbol(sym):
        v = d_raw.get(sym)
        stages = _normalize_value(v) if v is not None else None
        return stages if stages else list(default_stages)

    if len(symbols_cli) == 1 and symbols_cli[0] == "ALL":
        pos0 = list_positions(account_hash, client)
        symbols = sorted(list(pos0.keys()))
    else:
        symbols = symbols_cli

    last_order_ts: Dict[str, float] = {}
    pending: Dict[str, List[Dict]] = {}

    print(
        f"[RUN] Sell-High: {', '.join(symbols)}; interval={interval_sec}s; cooldown={cooldown_sec}s; "
        f"confirm={confirm}; verbose={verbose}; hours={hours}; on_close={on_close}; tz={tz_name}; "
        f"vol_sell={vol_sell}; spread_max={SPREAD_MAX_PCT:.2f}%",
        flush=True
    )
    print(f"[TIME] local={datetime.now():%Y-%m-%d %H:%M:%S} | display={_now_et():%Y-%m-%d %H:%M:%S} {_ET_TZNAME}", flush=True)


    tzinfo = _tz(tz_name)
    session = _session_for_hours(hours)

    while True:
        try:
            # reload sell.dic if changed
            try:
                mt = Path(sell_dic_path).stat().st_mtime
            except Exception:
                mt = -1
            if mt != last_mtime:
                d_raw, default_stages = _reload_rules()
                last_mtime = mt
                if verbose:
                    print(f"[SELL.DIC] Reloaded at {datetime.now():%H:%M:%S}", flush=True)

            # market-hours gate
            now_local = datetime.now(tzinfo) if tzinfo else datetime.now()
            if hours == "extended":
                start_t, end_t = _time(4, 0), _time(20, 0)
            else:
                start_t, end_t = _time(9, 30), _time(16, 0)

            in_session = _is_weekday(now_local) and (start_t <= now_local.time() < end_t)
            if not in_session:
                if on_close == "exit":
                    print(f"[EXIT] code=7 reason=Market closed at {now_local:%Y-%m-%d %H:%M:%S %Z}", flush=True)
                    sys.exit(7)
                secs = _seconds_until_next_open(now_local, start_t)
                sleep_for = min(secs, max(5, interval_sec))
                wake = now_local + timedelta(seconds=sleep_for)
                if verbose:
                    print(f"[WHY] Market closed; next open in ~{sleep_for}s (until ~{wake:%H:%M:%S %Z}).", flush=True)
                print(f"[SLEEP] Market closed; sleeping {sleep_for}s (until ~{wake:%H:%M:%S %Z}).", flush=True)
                time.sleep(sleep_for)
                continue

            # watch pending orders
            for sym, orders in list(pending.items()):
                still_open = []
                for o in orders:
                    oid = o.get("oid")
                    oqty = o.get("qty", 0)
                    odoc = fetch_order(client, account_hash, oid) if oid else None
                    if odoc is None:
                        still_open.append(o)
                        continue
                    status = (odoc.get("status") or "").upper()
                    if order_filled(odoc):
                        fqty = filled_qty(odoc) or oqty
                        print(f"[FILL] {sym} filled qty={int(fqty)} id={oid} status={status}", flush=True)

                        # FIX #4: Rearm stages only after confirmed fill (and only for final stage orders)
                        if o.get("is_last"):
                            _reset_stage_state(sym)
                            print(f"[REARM] {sym} final stage filled; stages reset.", flush=True)

                        try:
                            pos_now = list_positions(account_hash, client)
                            cur_qty = int(float((pos_now.get(sym) or {}).get("qty") or 0))
                            print(f"[POS]  {sym} position now {cur_qty}", flush=True)
                        except Exception as e:
                            print(f"[WARN] position refresh failed after fill: {e}", flush=True)
                    else:
                        still_open.append(o)
                if still_open:
                    pending[sym] = still_open
                else:
                    pending.pop(sym, None)

            # fresh positions
            pos = list_positions(account_hash, client)

            for sym in symbols:
                # flat: still show heartbeat
                if sym not in pos:
                    try:
                        q = get_quote(client, sym)
                        bid, ask, last = q.get("bid"), q.get("ask"), q.get("last")
                        sp = spread_pct(bid, ask)
                    except Exception:
                        bid = ask = last = sp = None
                    heartbeat_sell(
                        sym,
                        last=last, bid=bid, ask=ask, avg=None, tp=None,
                        spread_pct_val=sp, pos_qty=0, cooldown_ok_flag=True, armed=False,
                        pending_sell_qty=0, stage_text="", extra="flat(no_position)"
                    )
                    continue

                if pos[sym]["qty"] < min_shares:
                    try:
                        q = get_quote(client, sym)
                        bid, ask, last = q.get("bid"), q.get("ask"), q.get("last")
                        sp = spread_pct(bid, ask)
                    except Exception:
                        bid = ask = last = sp = None
                    heartbeat_sell(
                        sym,
                        last=last, bid=bid, ask=ask, avg=pos[sym].get("avg"), tp=None,
                        spread_pct_val=sp, pos_qty=int(pos[sym]["qty"]), cooldown_ok_flag=True, armed=False,
                        pending_sell_qty=0, stage_text="", extra=f"skip(qty<{min_shares})"
                    )
                    continue

                # resolve stages & next unfired stage
                stages = _stages_for_symbol(sym)
                state = _load_stage_state(sym)
                fired = set(state.get("fired_idxs", []))

                next_idx = None
                for i, st in enumerate(stages):
                    if i not in fired:
                        next_idx = i
                        break
                if next_idx is None:
                    if verbose:
                        print(f"[WHY] {sym} all stages exhausted", flush=True)
                    continue

                st = stages[next_idx]
                raw_thr = float(st["thr_pct"])

                # optional dynamic sell threshold
                if vol_sell == "on":
                    atrp = _atr_pct(client, sym)
                    sell_pct, regime = _dyn_sell_adjust(raw_thr, atrp)
                else:
                    atrp = None
                    sell_pct, regime = raw_thr, "off"

                q = get_quote(client, sym)
                last, bid, ask, close = q.get("last"), q.get("bid"), q.get("ask"), q.get("close")
                avg = pos[sym].get("avg") or close

                if last is None or avg is None:
                    if verbose:
                        print(f"[WHY] {sym} missing price data (last={last}, avg={avg})", flush=True)
                    continue

                tp = float(avg) * (1.0 + sell_pct / 100.0)
                sp = spread_pct(bid, ask)
                pend_qty = sum(int(o.get("qty", 0)) for o in pending.get(sym, []))
                armed = bool(last and tp and last >= tp)

                try:
                    profit_pct = (last / avg - 1.0) * 100.0
                except Exception:
                    profit_pct = float("nan")

                stage_frac = st.get("frac")
                use_frac = float(stage_frac if stage_frac is not None else sell_frac)

                atr_txt = f"{atrp:.2f}%" if (atrp is not None) else "-"
                stage_text = (f"{next_idx+1}/{len(stages)} thr={sell_pct:.2f}% (raw={raw_thr:.2f}%) "
                              f"atr%={atr_txt} reg={regime} frac={use_frac:.2f} profit={profit_pct:.2f}%")

                extra = ""
                if not armed:
                    try:
                        extra = f"gap={(tp - last) / tp * 100.0:.2f}%"
                    except Exception:
                        pass

                heartbeat_sell(
                    sym,
                    last=last, bid=bid, ask=ask, avg=avg, tp=tp,
                    spread_pct_val=sp, pos_qty=int(pos[sym]["qty"]),
                    cooldown_ok_flag=cooldown_ok(sym), armed=armed,
                    pending_sell_qty=pend_qty, stage_text=stage_text, extra=extra
                )

                # gates
                now = time.time()
                stage_cd = int(st.get("cooldown_sec") or cooldown_sec)
                is_last_stage = (next_idx + 1 == len(stages))

                # diagnostics (pre-gate)
                print(
                    f"[CHK] {sym} in_hours={in_session} cd_ok={cooldown_ok(sym)} "
                    f"spread={sp:.2f}%<=max?{sp<=SPREAD_MAX_PCT} armed={armed} last_stage={is_last_stage} "
                    f"cooldown_rem={int(max(0, (last_order_ts.get(sym, 0)+stage_cd - now)))}",
                    flush=True
                )

                if sp > SPREAD_MAX_PCT:
                    print(f"[SKIP][SPREAD] {sym} spread {sp:.2f}% > {SPREAD_MAX_PCT:.2f}%", flush=True)
                    continue
                if not cooldown_ok(sym):
                    print(f"[SKIP][XCD] {sym} cross-cooldown active", flush=True)
                    continue
                if sym in last_order_ts and (now - last_order_ts[sym]) < stage_cd:
                    rem = int(stage_cd - (now - last_order_ts[sym]))
                    print(f"[SKIP][CD] {sym} per-symbol cooldown {rem}s remaining", flush=True)
                    continue
                if not armed:
                    print(f"[SKIP][TP] {sym} last={last:.2f} < tp={tp:.2f}", flush=True)
                    continue

                # === DUST-PROOF qty calc ===
                pos_qty = int(pos[sym]["qty"])
                if is_last_stage:
                    qty = pos_qty
                else:
                    qty = int(math.floor(pos_qty * max(0.01, min(1.0, use_frac))))
                    if qty < min_shares:
                        qty = min_shares
                    if qty > pos_qty:
                        qty = pos_qty

                if qty < min_shares:
                    print(f"[SKIP][QTY] {sym} qty<{min_shares} (pos={pos_qty})", flush=True)
                    continue

                # price (SELL): avoid a sell limit far BELOW market (can be rejected by broker risk checks).
                # We are already 'armed' (last >= tp), so set limit near market to get filled, while respecting tp and cap.
                cap_px = None
                if st.get("cap_abs") is not None:
                    cap_px = float(st["cap_abs"])
                elif st.get("cap_pct") is not None:
                    cap_px = float(avg) * (1.0 + float(st["cap_pct"]) / 100.0)

                # Prefer bid for sells, then last, then ask, then tp
                mkt = (bid or last or ask or tp)
                if mkt is None:
                    mkt = tp

                # Start at the higher of tp and slightly-below-market (improves fill probability)
                limit_px = max(tp, float(mkt) * 0.999)

                # Apply optional cap (upper bound)
                if cap_px is not None:
                    limit_px = min(limit_px, cap_px)

                limit_px = round(float(limit_px), 2)

                print(f"[SEND] SELL {sym} stage={next_idx+1}/{len(stages)} qty={qty} limit={limit_px:.2f}", flush=True)

                if confirm:
                    # FIX #2: session respects --hours (NORMAL vs SEAMLESS)
                    res = place_limit_sell(client, account_hash, sym, qty, limit_px, session=session)

                    oid = (res or {}).get("order_id")
                    status = (res or {}).get("status")
                    resp_body = (res or {}).get("resp_body")

                    # FIX #3: verify order exists before marking stage fired/cooldown
                    odoc = fetch_order(client, account_hash, oid) if oid else None
                    if not odoc:
                        print(f"[ERR] SELL {sym} order not retrievable after POST. Not marking stage fired. id={oid} status={status}", flush=True)
                        if resp_body is not None:
                            print(f"[ERR] Response body: {resp_body}", flush=True)
                        continue

                    # Check Schwab order status before firing stage (prevents phantom-fired stages)
                    ostatus = (odoc.get("status") or "").upper()
                    print(f"[ORDER] {sym} status={ostatus} id={oid}", flush=True)

                    # Only mark stage fired if Schwab reports the order is LIVE (or already filled)
                    LIVE_OK = {"WORKING","QUEUED","PENDING_ACTIVATION","ACCEPTED","FILLED","EXECUTED","COMPLETED"}
                    if ostatus and (ostatus not in LIVE_OK):
                        print(f"[ROLLBACK] {sym} NOT firing stage; order status={ostatus}", flush=True)
                        if resp_body is not None:
                            print(f"[ROLLBACK] resp_body={resp_body}", flush=True)
                        continue

                    # mark timers/state only after verified LIVE order
                    last_order_ts[sym] = now
                    mark_cooldown(sym)
                    _mark_stage_fired(sym, next_idx)

                    # enable pending tracking (so we can rearm on fill)
                    pending.setdefault(sym, []).append({"oid": oid, "qty": qty, "is_last": is_last_stage})

                    print(
                        f"[OK]  SELL {sym} STAGE {next_idx+1}/{len(stages)} thr={sell_pct:.2f}% "
                        f"qty={qty} limit={limit_px:.2f} id={oid} status={status}",
                        flush=True
                    )
                    log_event(
                        strategy="TP", side=str("SELL"), symbol=sym, mode="tp", baseline="avg_cost",
                        threshold_pct=sell_pct, last=last, close=close,
                        action=f"PLACED_STAGE_{next_idx+1}", qty=qty,
                        order_id=oid, order_status=str(status),
                        fill_price=None, fill_value=None, notes=f"limit={limit_px} raw_thr={raw_thr} vol_reg={regime} atr%={atr_txt} session={session}"
                    )

                    # Position refresh after placement (may not change until fill)
                    try:
                        pos_now = list_positions(account_hash, client)
                        cur_qty = int(float((pos_now.get(sym) or {}).get("qty") or 0))
                        print(f"[POS]  {sym} position now {cur_qty}", flush=True)
                    except Exception as e:
                        print(f"[WARN] position refresh failed: {e}", flush=True)
                else:
                    print(
                        f"[DRY] SELL {sym} STAGE {next_idx+1}/{len(stages)} thr={sell_pct:.2f}% "
                        f"qty={qty} limit={limit_px:.2f}",
                        flush=True
                    )
                    return

            time.sleep(interval_sec)

        except KeyboardInterrupt:
            print("\n[STOP] by user.", flush=True)
            break
        except Exception as e:
            print("[WARN] loop exception:", e, flush=True)
            time.sleep(5)

# -------------- entry --------------
if __name__ == "__main__":
    main()
