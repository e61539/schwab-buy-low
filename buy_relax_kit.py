import os, json, time
from pathlib import Path

_OVERRIDES = {}
APP_ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parent)).resolve()
_OV_PATH = str(APP_ROOT / "config" / "sym_overrides.json")
_OV_MTIME = None

def _load_overrides():
    global _OVERRIDES, _OV_MTIME
    try:
        m = os.path.getmtime(_OV_PATH)
        if _OV_MTIME is None or m != _OV_MTIME:
            with open(_OV_PATH, "r", encoding="utf-8") as f:
                _OVERRIDES = json.load(f)
            _OV_MTIME = m
    except Exception:
        _OVERRIDES = {}

def ov(sym, key, default=None):
    _load_overrides()
    d = _OVERRIDES or {}
    if isinstance(d, dict):
        if isinstance(d.get(sym), dict) and key in d[sym]:
            return d[sym][key]
        if isinstance(d.get("DEFAULT"), dict) and key in d["DEFAULT"]:
            return d["DEFAULT"][key]
    return default

def eff_max_slippage(symbol, global_max_slippage):
    v = ov(symbol, "max_slippage", None)
    return float(v) if v is not None else float(global_max_slippage)

def eff_min_usd(symbol, fallback=50.0):
    v = ov(symbol, "min_usd", None)
    return float(v) if v is not None else float(fallback)

def eff_usd_cap_abs(symbol):
    v = ov(symbol, "usd_cap_abs", None)
    return None if v in (None, "null") else float(v)

def eff_exp_cap(symbol, default=None):
    v = ov(symbol, "exp_cap", None)
    return float(v) if v is not None else (float(default) if default is not None else None)

def spread_gate(symbol, bid, ask, mid, global_max_slip, log):
    """Return (ok: bool, max_slip_eff: float, note: str)"""
    if bid is None or ask is None or mid in (None, 0):
        return True, eff_max_slippage(symbol, global_max_slip), "[SPREAD] missing quotes; letting it pass"
    bps = abs(ask - bid) / float(mid)
    max_slip_eff = eff_max_slippage(symbol, global_max_slip)
    if bps > max_slip_eff:
        return False, max_slip_eff, f"[HOLD] bps={bps:.4f} > max={max_slip_eff:.4f} (sym override)"
    return True, max_slip_eff, f"[OK] bps within {max_slip_eff:.4f}"

def partial_size(
    *, symbol, price, desired_shares, total_equity_usd,
    current_mv_usd, headroom_usd, usd_per_symbol_cap, exp_cap_default,
    log
):
    """
    Returns (buy_shares:int, budget_used:float, note:str).
    Allows partial stage as long as it clears min_usd and ≥1 share.
    """
    if price in (None, 0):
        return 0, 0.0, "[HOLD] no price"

    min_usd = eff_min_usd(symbol, 50.0)
    stage_usd = float(desired_shares) * float(price)

    budget = stage_usd

    # Headroom clamp (e.g., based on sym_cap%)
    if headroom_usd is not None:
        budget = min(budget, max(float(headroom_usd), 0.0))

    # Global UsdPerSymbol cap clamp
    if usd_per_symbol_cap and usd_per_symbol_cap > 0:
        remain = max(float(usd_per_symbol_cap) - float(current_mv_usd), 0.0)
        budget = min(budget, remain)

    # Absolute per-symbol cap from overrides
    abs_cap = eff_usd_cap_abs(symbol)
    if abs_cap is not None:
        remain = max(float(abs_cap) - float(current_mv_usd), 0.0)
        budget = min(budget, remain)

    # Optional exposure cap override (fraction of equity, e.g., 0.06 = 6%)
    exp_cap = eff_exp_cap(symbol, exp_cap_default)
    if exp_cap is not None and total_equity_usd and total_equity_usd > 0:
        cap_mv = float(total_equity_usd) * float(exp_cap)
        remain = max(cap_mv - float(current_mv_usd), 0.0)
        budget = min(budget, remain)

    if budget < min_usd:
        return 0, budget, (f"[HOLD] budget=${budget:.2f} < min_usd=${min_usd:.2f} "
                           f"(tight caps/headroom; overrides active)")

    max_shares_by_budget = int(budget // float(price))
    if max_shares_by_budget <= 0:
        return 0, budget, f"[HOLD] budget translates to 0 shares at ${price:.2f}"

    buy_shares = min(int(desired_shares), max_shares_by_budget)
    used = buy_shares * float(price)
    return buy_shares, used, (f"[PARTIAL-OK] stage=${stage_usd:.2f} -> "
                              f"budget=${budget:.2f} -> buy_shares={buy_shares} "
                              f"(min_usd=${min_usd:.2f})")
