# Unified trade logger for Buy Low / Sell High / Runner
# - log_event(...) is backward-compatible and tolerates extra kwargs (e.g., dip_pct, trigger)

import csv, json, os, re
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

APP_ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parent)).resolve()
RUNTIME_DIR = APP_ROOT / "runtime"
DEFAULT_CSV = str(RUNTIME_DIR / "trades_log.csv")
STATE_JSON  = str(RUNTIME_DIR / "trades_log_seen.json")   # remembers seen rows for de-dupe

def _ensure_parent(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def _load_state(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return {}
    return {}

def _save_state(path: Path, data: Dict[str, Any]):
    _ensure_parent(path)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

def _mk_key(row: Dict[str, Any]) -> str:
    # Prefer stable order_id if present
    oid = str(row.get("order_id") or "").strip()
    if oid:
        return f"id:{oid}"
    # Fallback: round to minute and include core fields/price
    ts = str(row.get("timestamp") or datetime.utcnow().strftime("%Y-%m-%d %H:%M"))
    ts_min = ts[:16]  # YYYY-MM-DD HH:MM
    price = row.get("price") or row.get("limit_price") or ""
    try:
        price_q = f"{float(price):.4f}"
    except Exception:
        price_q = str(price)
    return f"{ts_min}|{row.get('source')}|{row.get('action')}|{row.get('symbol')}|{row.get('qty')}|{price_q}"

def append_trade(
    csv_path: str = DEFAULT_CSV,
    *,
    source: str,
    action: str,            # BUY / SELL
    symbol: str,
    qty: float,
    order_style: str = "",  # "limit" / "market" / ""
    price: Optional[float] = None,
    limit_price: Optional[float] = None,
    refprice: Optional[str] = None,
    order_id: Optional[str] = None,
    status: str = "submitted",   # submitted/filled/canceled/rejected/expired
    account: Optional[str] = None,
    notes: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> bool:
    """Append one trade row to CSV if not seen before. Returns True if row was written."""
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "action": action.upper(),
        "symbol": symbol.upper(),
        "qty": qty,
        "order_style": (order_style or "").lower(),
        "price": price if price is not None else "",
        "limit_price": limit_price if limit_price is not None else "",
        "refprice": refprice or "",
        "order_id": order_id or "",
        "status": (status or "").lower(),
        "account": account or "",
        "notes": notes or "",
    }
    if extra:
        for k, v in extra.items():
            if k not in row:
                row[k] = v

    key = _mk_key(row)
    state_path = Path(STATE_JSON)
    st = _load_state(state_path)
    seen = set(st.get("keys", []))
    if key in seen:
        return False
    # Trim memory
    if len(seen) > 2000:
        seen = set(list(seen)[-1500:])

    csv_p = Path(csv_path)
    _ensure_parent(csv_p)
    header = ["timestamp","source","action","symbol","qty","order_style","price","limit_price","refprice","order_id","status","account","notes"]
    write_header = not csv_p.exists()
    with csv_p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)

    seen.add(key)
    st["keys"] = list(seen)
    _save_state(state_path, st)
    return True

# ---------- Helpers used by your scripts ----------
import re

_order_id_pat = re.compile(r"\b(orderId|id)\s*=\s*([A-Za-z0-9\-]+)")

def parse_order_id(obj) -> str:
    if obj is None:
        return ""

    # 1) Response Location header is best if present
    if hasattr(obj, "headers") and obj.headers:
        loc = obj.headers.get("Location") or obj.headers.get("location")
        if loc:
            oid = loc.rstrip("/").split("/")[-1].strip()
            if oid:
                return oid

    # 2) If response has json, try common keys
    if hasattr(obj, "json"):
        try:
            j = obj.json()
            if isinstance(j, dict):
                v = j.get("orderId") or j.get("id") or j.get("order_id")
                if v is not None:
                    return str(v)
        except Exception:
            pass

    # 3) Fallback regex on text
    text = getattr(obj, "text", None)
    if text is None:
        text = obj.decode("utf-8", "ignore") if isinstance(obj, bytes) else str(obj)

    m = _order_id_pat.search(text or "")
    return m.group(2) if m else ""

def extract_fill(text: Any):
    # Accept dict/obj/string; search for a standard "Filled BUY/SELL X SYM @ PX"
    s = str(text) if not isinstance(text, str) else text
    m = re.search(r"\b(Filled|EXECUTED)\b.*?\b(BUY|SELL)\b\s+(\d+(?:\.\d+)?)\s+([A-Z][A-Z0-9\-\._]{1,9})\s*@\s*(\d+(?:\.\d+)?)",
                  s, re.I)
    if not m:
        return None, None, None
    side = m.group(2).upper()
    qty  = float(m.group(3))
    sym  = m.group(4).upper()
    px   = float(m.group(5))
    return side, sym, (qty, px)

def log_event(
    *,
    side: str, symbol: str,
    mode: str = "", baseline: str = "",
    threshold_pct: float = None,
    last: float = None, close: float = None,
    action: str = "submitted", qty: float = 0.0,
    order_id: str = "", order_status: str = "",
    fill_price: float = None, fill_value: float = None,
    notes: str = "", account: str = "",
    **kwargs    # tolerate extras like dip_pct, trigger, refprice, etc.
):
    """
    Back-compat logger used in your scripts. Any unknown kwargs are folded into the 'notes' field.
    """
    parts = []
    if mode: parts.append(f"mode={mode}")
    if baseline: parts.append(f"baseline={baseline}")
    if threshold_pct is not None: parts.append(f"thr={threshold_pct}")
    if last is not None: parts.append(f"last={last}")
    if close is not None: parts.append(f"close={close}")
    if order_status: parts.append(f"status={order_status}")
    # include common extras if present
    for k in ("dip_pct", "trigger", "refprice"):
        if k in kwargs:
            parts.append(f"{k}={kwargs[k]}")
    note = "; ".join(parts + ([notes] if notes else []))

    append_trade(
        source=("sell_high" if side.upper() == "SELL" else "buy_low"),
        action=side, symbol=symbol, qty=qty,
        order_style=("limit" if fill_price is not None else ""),
        price=(fill_price or ""),
        limit_price=(fill_price or ""),
        order_id=(order_id or ""),
        status=(order_status or action or "submitted"),
        account=(account or ""),
        notes=note
    )
