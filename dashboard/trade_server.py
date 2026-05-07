from __future__ import annotations

import csv
import asyncio
import re
import io
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Query
from pydantic import BaseModel, Field, field_validator

# ============================================================
# Configuration
# ============================================================

# Phase 2 path hardening: resolve repo-local paths independently of cwd.
ROOT = Path(os.getenv("BUYLOW_HOME", Path(__file__).resolve().parents[1])).resolve()
CONFIG_DIR = ROOT / "config"
RUNTIME_DIR = ROOT / "runtime"
LOCKS_DIR = RUNTIME_DIR / "locks"
CACHE_DIR = RUNTIME_DIR / "cache"
STATE_DIR = RUNTIME_DIR / "state"
for _path in (ROOT, ROOT / "dashboard"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

TRADE_API_KEY = os.getenv("TRADE_API_KEY", "").strip()

TRADE_SCRIPT = Path(os.getenv("TRADE_SCRIPT", r"C:\Users\cheng_hamn078\dashboard\Trade.py"))
POSITIONS_SCRIPT = Path(os.getenv("POSITIONS_SCRIPT", r"C:\Users\cheng_hamn078\dashboard\positions.py"))
DEFAULT_ACCT = os.getenv("TRADE_DEFAULT_ACCT", "IRA1").strip() or "IRA1"
RISK_CONFIG_PATH = Path(os.getenv("RISK_CONFIG_PATH", r"C:\temp\risk_config.json"))

QUOTE_TIMEOUT_SECONDS = 8
PREVIEW_TTL_SECONDS = 300
CONFIRM_CODE_LENGTH = 6
SUBPROCESS_TIMEOUT_SECONDS = 45
POSITIONS_CACHE_TTL_SECONDS = float(os.getenv("POSITIONS_CACHE_TTL_SECONDS", "15"))
OPEN_ORDERS_CACHE_TTL_SECONDS = float(os.getenv("OPEN_ORDERS_CACHE_TTL_SECONDS", "10"))

ALLOWED_SYMBOLS = {"SPY", "QQQ", "GLD", "TQQQ", "COST"}
HARD_MAX_QTY_PER_ORDER = 25
HARD_MAX_NOTIONAL_PER_ORDER = 25000.0

PENDING_PREVIEWS: Dict[str, dict] = {}
PREVIEW_IDEMPOTENCY: Dict[str, dict] = {}
CONFIRM_IDEMPOTENCY: Dict[str, dict] = {}

_POSITIONS_PAYLOAD_CACHE: dict = {"ts": 0.0, "data": None}
_OPEN_ORDERS_CACHE: dict = {"ts": 0.0, "data": None}

_RISK_CONFIG_CACHE: dict = {}
_RISK_CONFIG_MTIME: Optional[float] = None
_RISK_CONFIG_LAST_LOAD_TS: float = 0.0

LOG_LEVEL = os.getenv("TRADE_SERVER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("trade_server")

app = FastAPI(title="Trade Server", version="2.7.0")

_WINERROR64_LAST_LOG_TS = 0.0


def _is_windows_accept_disconnect(exc: BaseException | None, message: str = "") -> bool:
    if not isinstance(exc, OSError):
        return False
    winerror = getattr(exc, "winerror", None)
    if winerror != 64:
        return False
    text = f"{message} {exc}".lower()
    return "accept" in text or "network name is no longer available" in text


def _install_windows_accept_exception_handler() -> None:
    if os.name != "nt":
        return

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def handler(loop, context):
        global _WINERROR64_LAST_LOG_TS
        exc = context.get("exception")
        message = str(context.get("message") or "")
        if _is_windows_accept_disconnect(exc, message):
            now = time.time()
            if now - _WINERROR64_LAST_LOG_TS >= 60:
                _WINERROR64_LAST_LOG_TS = now
                logger.warning(
                    "Ignoring transient Windows socket accept disconnect "
                    "(WinError 64). This is treated as client/network noise, "
                    "not Schwab/auth/trading failure."
                )
            return
        if previous_handler:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


@app.on_event("startup")
async def startup_health_logging() -> None:
    _install_windows_accept_exception_handler()
    logger.info(
        "trade_server healthy startup host_process=%s trade_script_exists=%s "
        "positions_script_exists=%s api_key_configured=%s",
        os.getpid(),
        TRADE_SCRIPT.exists(),
        POSITIONS_SCRIPT.exists(),
        bool(TRADE_API_KEY),
    )


# ============================================================
# Models
# ============================================================

class PreviewRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: str
    qty: int = Field(..., gt=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol_field(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("side")
    @classmethod
    def normalize_side(cls, v: str) -> str:
        side = v.strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        return side


class PreviewResponse(BaseModel):
    ok: bool
    preview_id: str
    confirm_code: str
    symbol: str
    side: str
    qty: int
    expires_in_sec: int
    acct: str


class ConfirmRequest(BaseModel):
    preview_id: str
    confirm_code: str


class ConfirmResponse(BaseModel):
    ok: bool
    preview_id: str
    symbol: str
    side: str
    qty: int
    status: str
    broker_result: dict


class OrderLookupResponse(BaseModel):
    ok: bool
    order_id: str
    acct: str
    broker_result: dict
    summary: dict


class OrderStatusResponse(BaseModel):
    ok: bool
    order_id: str
    acct: str
    symbol: Optional[str] = None
    side: Optional[str] = None
    status: Optional[str] = None
    filled: bool
    filled_qty: Optional[float] = None
    fill_price: Optional[float] = None
    entered_time: Optional[str] = None
    close_time: Optional[str] = None


class PositionRow(BaseModel):
    symbol: str
    qty: float
    avg_cost: float
    market_price: float
    market_value: float
    cost_basis: float
    gain_loss: float
    gain_loss_pct: Optional[float] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    dist_to_52w_high_dollar: Optional[float] = None
    dist_to_52w_high_pct: Optional[float] = None
    dist_from_52w_low_dollar: Optional[float] = None
    dist_from_52w_low_pct: Optional[float] = None


class PositionsSummary(BaseModel):
    market_value: float
    cost_basis: float
    gain_loss: float
    gain_loss_pct: Optional[float] = None


class PositionsResponse(BaseModel):
    ok: bool
    count: int
    positions: List[PositionRow]
    summary: PositionsSummary

    asset_total: float
    cash_available: Optional[float] = None
    settled_cash: Optional[float] = None
    buying_power: Optional[float] = None
    total_account_value: Optional[float] = None
    pending_buy_notional: Optional[float] = None
    free_cash_after_pending: Optional[float] = None

# ============================================================
# Helpers
# ============================================================

def get_open_orders() -> list[dict]:
    """
    Return open/working broker orders from Trade.py.
    """
    now = time.time()
    cached_orders = _OPEN_ORDERS_CACHE.get("data")
    if (
        cached_orders is not None
        and OPEN_ORDERS_CACHE_TTL_SECONDS > 0
        and now - float(_OPEN_ORDERS_CACHE.get("ts") or 0.0) < OPEN_ORDERS_CACHE_TTL_SECONDS
    ):
        return cached_orders

    try:
        cmd = [
            sys.executable,
            str(TRADE_SCRIPT),
            "--get-orders",
            "--acct",
            DEFAULT_ACCT,
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,   # 🔥 suppress noise
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            logger.warning(
                "get_open_orders failed rc=%s stdout=%s stderr=%s",
                result.returncode,
                (result.stdout or "").strip(),
                (result.stderr or "").strip(),
            )
            return []

        stdout = result.stdout or ""
        if not stdout.strip():
            logger.warning("get_open_orders got empty stdout")
            return []

        try:
            payload = _extract_final_json_object(stdout)
        except Exception as e:
            logger.warning(
                "get_open_orders JSON extract failed: %s ; stdout=%r",
                e,
                stdout[:2000],
            )
            return []

        if isinstance(payload, list):
            orders = [x for x in payload if isinstance(x, dict)]
            _OPEN_ORDERS_CACHE.update({"ts": now, "data": orders})
            return orders

        if isinstance(payload, dict):
            if isinstance(payload.get("orders"), list):
                orders = [x for x in payload["orders"] if isinstance(x, dict)]
                _OPEN_ORDERS_CACHE.update({"ts": now, "data": orders})
                return orders

            result_block = payload.get("result")
            if isinstance(result_block, dict) and isinstance(result_block.get("orders"), list):
                orders = [x for x in result_block["orders"] if isinstance(x, dict)]
                _OPEN_ORDERS_CACHE.update({"ts": now, "data": orders})
                return orders

            if isinstance(payload.get("data"), list):
                orders = [x for x in payload["data"] if isinstance(x, dict)]
                _OPEN_ORDERS_CACHE.update({"ts": now, "data": orders})
                return orders

        logger.warning("get_open_orders unexpected payload shape: %r", payload)
        return []

    except Exception as e:
        logger.warning("get_open_orders exception: %s", e)
        return []
        
def calc_broker_pending_buy_notional(orders) -> float:
    total = 0.0

    for o in orders:
        status = str(o.get("status", "")).upper()
        side = str(o.get("side", "")).upper()
        symbol = str(o.get("symbol", "")).upper()
        order_type = str(o.get("order_type", "")).upper()

        # ✅ Include ALL active buy states
        if status not in ("OPEN", "WORKING", "QUEUED", "PENDING", "ACCEPTED", "PENDING_ACTIVATION"):
            continue

        if side != "BUY":
            continue

        qty = float(o.get("quantity", 0) or 0)

        # 🔥 SWVXX special handling (CRITICAL)
        if symbol == "SWVXX" and order_type == "MARKET":
            total += qty
            continue

        price = o.get("price") or 0
        total += qty * float(price)

    return round(total, 2)
        
def calc_pending_buy_notional() -> float:
    """
    Estimate pending BUY notional from unconfirmed previews.
    This captures iPhone trades not yet confirmed.
    """
    total = 0.0

    for p in PENDING_PREVIEWS.values():
        if p.get("consumed"):
            continue

        if p.get("side") != "BUY":
            continue

        qty = float(p.get("qty", 0))

        try:
            est_price, _ = resolve_estimated_price(p["symbol"], load_risk_config())
        except Exception:
            est_price = 0

        total += qty * est_price

    return round(total, 2)

def require_server_configuration() -> None:
    if not TRADE_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration: TRADE_API_KEY is not set."
        )

    if not os.getenv("app_key", "").strip():
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration: app_key is not set."
        )

    if not os.getenv("app_secret", "").strip():
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration: app_secret is not set."
        )

    if not TRADE_SCRIPT.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Server misconfiguration: Trade.py not found at {TRADE_SCRIPT}"
        )


def require_api_key(x_api_key: Optional[str]) -> None:
    require_server_configuration()
    if not x_api_key or x_api_key.strip() != TRADE_API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def cleanup_expired_previews() -> None:
    now = time.time()
    expired = [
        preview_id
        for preview_id, payload in PENDING_PREVIEWS.items()
        if payload["expires_at"] < now
    ]
    for preview_id in expired:
        PENDING_PREVIEWS.pop(preview_id, None)


def make_confirm_code() -> str:
    low = 10 ** (CONFIRM_CODE_LENGTH - 1)
    high = (10 ** CONFIRM_CODE_LENGTH) - 1
    return str(secrets.randbelow(high - low + 1) + low)


def require_allowed_symbol(symbol: str) -> None:
    if symbol not in ALLOWED_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Symbol {symbol} is not in ALLOWED_SYMBOLS"
        )


def require_safe_qty_hard_limit(qty: int) -> None:
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be > 0")
    if qty > HARD_MAX_QTY_PER_ORDER:
        raise HTTPException(
            status_code=400,
            detail=f"qty exceeds HARD_MAX_QTY_PER_ORDER={HARD_MAX_QTY_PER_ORDER}"
        )


def preview_idempotency_key(request_key: str) -> str:
    return f"preview:{request_key}"


def confirm_idempotency_key(request_key: str) -> str:
    return f"confirm:{request_key}"


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip().replace(",", "")
    if not s:
        return default

    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return default

    try:
        return float(m.group(0))
    except Exception:
        return default


def safe_round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


# ============================================================
# Risk config loading with reload
# ============================================================

def default_risk_config() -> dict:
    return {
        "MAX_NOTIONAL_PER_ORDER": 3000.0,
        "MAX_QTY_PER_ORDER": 5,
        "DEFAULT_PRICE_LIMIT": 1000.0,
        "BLOCK_OUTSIDE_MARKET_HOURS": False,
        "ALLOW_AFTER_HOURS_WITH_FALLBACK_PRICE": True,
        "REQUIRE_LIVE_QUOTE_DURING_MARKET_HOURS": True,
        "REQUIRE_LIVE_QUOTE_AFTER_HOURS": False,
        "PRICE_GUARDS": {},
        "MAX_NOTIONAL_BY_SYMBOL": {},
    }


def _normalize_risk_config(raw: dict) -> dict:
    cfg = default_risk_config()

    if not isinstance(raw, dict):
        return cfg

    cfg["MAX_NOTIONAL_PER_ORDER"] = float(
        raw.get("MAX_NOTIONAL_PER_ORDER", cfg["MAX_NOTIONAL_PER_ORDER"])
    )
    cfg["MAX_QTY_PER_ORDER"] = int(
        raw.get("MAX_QTY_PER_ORDER", cfg["MAX_QTY_PER_ORDER"])
    )
    cfg["DEFAULT_PRICE_LIMIT"] = float(
        raw.get("DEFAULT_PRICE_LIMIT", cfg["DEFAULT_PRICE_LIMIT"])
    )
    cfg["BLOCK_OUTSIDE_MARKET_HOURS"] = bool(
        raw.get("BLOCK_OUTSIDE_MARKET_HOURS", cfg["BLOCK_OUTSIDE_MARKET_HOURS"])
    )
    cfg["ALLOW_AFTER_HOURS_WITH_FALLBACK_PRICE"] = bool(
        raw.get(
            "ALLOW_AFTER_HOURS_WITH_FALLBACK_PRICE",
            cfg["ALLOW_AFTER_HOURS_WITH_FALLBACK_PRICE"],
        )
    )
    cfg["REQUIRE_LIVE_QUOTE_DURING_MARKET_HOURS"] = bool(
        raw.get(
            "REQUIRE_LIVE_QUOTE_DURING_MARKET_HOURS",
            cfg["REQUIRE_LIVE_QUOTE_DURING_MARKET_HOURS"],
        )
    )
    cfg["REQUIRE_LIVE_QUOTE_AFTER_HOURS"] = bool(
        raw.get(
            "REQUIRE_LIVE_QUOTE_AFTER_HOURS",
            cfg["REQUIRE_LIVE_QUOTE_AFTER_HOURS"],
        )
    )

    price_guards = raw.get("PRICE_GUARDS", {})
    if isinstance(price_guards, dict):
        cfg["PRICE_GUARDS"] = {
            str(k).upper(): float(v)
            for k, v in price_guards.items()
        }

    max_notional_by_symbol = raw.get("MAX_NOTIONAL_BY_SYMBOL", {})
    if isinstance(max_notional_by_symbol, dict):
        cfg["MAX_NOTIONAL_BY_SYMBOL"] = {
            str(k).upper(): float(v)
            for k, v in max_notional_by_symbol.items()
        }

    return cfg


def load_risk_config(force: bool = False) -> dict:
    global _RISK_CONFIG_CACHE, _RISK_CONFIG_MTIME, _RISK_CONFIG_LAST_LOAD_TS

    if not RISK_CONFIG_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Risk config not found: {RISK_CONFIG_PATH}"
        )

    try:
        mtime = RISK_CONFIG_PATH.stat().st_mtime
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to stat risk config: {e}"
        )

    should_reload = force or (_RISK_CONFIG_MTIME is None) or (mtime != _RISK_CONFIG_MTIME)

    if not should_reload:
        return _RISK_CONFIG_CACHE

    try:
        with open(RISK_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cfg = _normalize_risk_config(raw)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load risk config {RISK_CONFIG_PATH}: {e}"
        )

    _RISK_CONFIG_CACHE = cfg
    _RISK_CONFIG_MTIME = mtime
    _RISK_CONFIG_LAST_LOAD_TS = time.time()

    return _RISK_CONFIG_CACHE


def get_price_limit(symbol: str, cfg: dict) -> float:
    return float(
        cfg.get("PRICE_GUARDS", {}).get(
            symbol,
            cfg.get("DEFAULT_PRICE_LIMIT", 999999.0)
        )
    )


def get_max_qty(symbol: str, cfg: dict) -> int:
    return int(cfg.get("MAX_QTY_PER_ORDER", 0))


def get_max_notional(symbol: str, cfg: dict) -> float:
    per_symbol = cfg.get("MAX_NOTIONAL_BY_SYMBOL", {})
    if symbol in per_symbol:
        return float(per_symbol[symbol])
    return float(cfg.get("MAX_NOTIONAL_PER_ORDER", 0.0))


# ============================================================
# Quote / market-hour guards
# ============================================================

def _parse_positive_float(value: str) -> Optional[float]:
    try:
        px = float(str(value).strip())
        if px > 0:
            return px
    except Exception:
        pass
    return None


def _looks_like_header(row: list[str]) -> bool:
    joined = ",".join(x.strip().upper() for x in row)
    return "SYMBOL" in joined and ("CLOSE" in joined or "LAST" in joined or "PRICE" in joined)


def _normalize_rows(raw: str) -> list[list[str]]:
    text = raw.strip()
    if not text:
        return []

    rows: list[list[str]] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        cleaned = [col.strip() for col in row]
        if any(col != "" for col in cleaned):
            rows.append(cleaned)
    return rows


def _extract_price_from_known_layout(row: list[str]) -> Optional[float]:
    if len(row) >= 7:
        px = _parse_positive_float(row[6])
        if px is not None:
            return px
    return None


def _extract_price_by_best_effort(row: list[str]) -> Optional[float]:
    for value in reversed(row):
        s = value.strip()
        if not s:
            continue

        upper = s.upper()
        if upper.endswith(".US"):
            continue
        if s.isdigit() and len(s) == 8:
            continue
        if s.isdigit() and len(s) in {4, 6}:
            continue

        px = _parse_positive_float(s)
        if px is None:
            continue
        if px >= 1_000_000:
            continue
        return px

    return None


def parse_quote_price(raw: str, symbol: str) -> float:
    rows = _normalize_rows(raw)
    if not rows:
        raise ValueError(f"Quote lookup returned empty data for {symbol}")

    data_rows = rows
    if _looks_like_header(rows[0]):
        data_rows = rows[1:]

    if not data_rows:
        raise ValueError(f"Quote lookup returned header without data for {symbol}")

    for row in data_rows:
        px = _extract_price_from_known_layout(row)
        if px is not None:
            return px

    for row in data_rows:
        px = _extract_price_by_best_effort(row)
        if px is not None:
            return px

    raise ValueError(f"Quote lookup returned unrecognized format for {symbol}: {raw[:300]!r}")


def get_quote_last_price(symbol: str) -> float:
    url = f"https://stooq.com/q/l/?s={symbol.lower()}.us&i=d"
    logger.info("QUOTE lookup start symbol=%s url=%s", symbol, url)

    try:
        with urllib.request.urlopen(url, timeout=QUOTE_TIMEOUT_SECONDS) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", errors="replace").strip()
    except urllib.error.URLError as e:
        raise HTTPException(status_code=503, detail=f"Quote lookup failed for {symbol}: {e}")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Quote lookup failed for {symbol}: {e}")

    logger.info(
        "QUOTE raw response symbol=%s content_type=%s raw=%r",
        symbol,
        content_type,
        raw[:300],
    )

    raw_upper = raw[:200].upper()
    if "<!DOCTYPE" in raw_upper or "<HTML" in raw_upper:
        raise HTTPException(
            status_code=503,
            detail=f"Quote lookup returned HTML instead of quote data for {symbol}"
        )

    try:
        px = parse_quote_price(raw, symbol)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))

    logger.info("QUOTE lookup success symbol=%s est_price=%.4f", symbol, px)
    return px


def get_quote_snapshot(symbol: str) -> dict:
    raw_price = get_quote_last_price(symbol)
    return {
        "last_price": raw_price,
        "close_price": raw_price,
        "week52_high": None,
        "week52_low": None,
    }


def is_regular_market_hours_et() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hhmm = now.hour * 100 + now.minute
    return 930 <= hhmm < 1600


def enforce_market_hours(cfg: dict) -> None:
    if not bool(cfg.get("BLOCK_OUTSIDE_MARKET_HOURS", False)):
        return

    if not is_regular_market_hours_et():
        raise HTTPException(
            status_code=400,
            detail="Order blocked outside regular market hours"
        )


def enforce_price_guard(symbol: str, est_price: float, cfg: dict) -> float:
    limit_price = get_price_limit(symbol, cfg)

    if est_price > limit_price:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Price guard blocked order for {symbol}: "
                f"estimated price {est_price:.2f} exceeds limit {limit_price:.2f}"
            )
        )

    return limit_price


def enforce_qty_guard(symbol: str, qty: int, cfg: dict) -> int:
    max_qty = get_max_qty(symbol, cfg)

    if max_qty <= 0:
        raise HTTPException(status_code=500, detail="Invalid MAX_QTY_PER_ORDER in risk config")

    if qty > max_qty:
        raise HTTPException(
            status_code=400,
            detail=f"qty {qty} exceeds MAX_QTY_PER_ORDER {max_qty}"
        )

    return max_qty


def enforce_notional_guard(symbol: str, qty: int, est_price: float, cfg: dict) -> tuple[float, float]:
    max_notional = get_max_notional(symbol, cfg)

    if max_notional <= 0:
        raise HTTPException(status_code=500, detail="Invalid notional cap in risk config")

    if max_notional > HARD_MAX_NOTIONAL_PER_ORDER:
        max_notional = HARD_MAX_NOTIONAL_PER_ORDER

    est_notional = qty * est_price
    if est_notional > max_notional:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Max risk blocked order for {symbol}: "
                f"estimated notional {est_notional:.2f} exceeds cap {max_notional:.2f}"
            )
        )

    return est_notional, max_notional


def resolve_estimated_price(symbol: str, cfg: dict) -> tuple[float, str]:
    market_open = is_regular_market_hours_et()
    fallback_price = get_price_limit(symbol, cfg)

    require_live_quote_during_market = bool(cfg.get("REQUIRE_LIVE_QUOTE_DURING_MARKET_HOURS", True))
    require_live_quote_after_hours = bool(cfg.get("REQUIRE_LIVE_QUOTE_AFTER_HOURS", False))
    allow_after_hours_fallback = bool(cfg.get("ALLOW_AFTER_HOURS_WITH_FALLBACK_PRICE", True))

    if market_open:
        if not require_live_quote_during_market:
            logger.warning(
                "QUOTE bypassed during market hours symbol=%s using price_limit=%.2f",
                symbol,
                fallback_price,
            )
            return fallback_price, "price_limit_fallback_market_hours"

        try:
            return get_quote_last_price(symbol), "live_quote"
        except HTTPException as e:
            logger.warning("QUOTE failed during market hours symbol=%s detail=%s", symbol, e.detail)
            raise HTTPException(
                status_code=503,
                detail=f"Live quote required during market hours but failed for {symbol}: {e.detail}"
            ) from e

    if require_live_quote_after_hours:
        try:
            return get_quote_last_price(symbol), "live_quote_after_hours"
        except HTTPException as e:
            logger.warning("QUOTE failed after hours symbol=%s detail=%s", symbol, e.detail)
            raise HTTPException(
                status_code=503,
                detail=f"Live quote required after hours but failed for {symbol}: {e.detail}"
            ) from e

    if allow_after_hours_fallback:
        logger.warning(
            "After-hours quote bypass symbol=%s using price_limit=%.2f",
            symbol,
            fallback_price,
        )
        return fallback_price, "price_limit_fallback_after_hours"

    raise HTTPException(
        status_code=400,
        detail=f"Order blocked after hours for {symbol}: no live quote required and fallback disabled"
    )


# ============================================================
# Positions helpers
# ============================================================

def run_positions_script(force_refresh: bool = False) -> Dict[str, Any]:
    now = time.time()
    cached_payload = _POSITIONS_PAYLOAD_CACHE.get("data")
    if (
        not force_refresh
        and cached_payload is not None
        and POSITIONS_CACHE_TTL_SECONDS > 0
        and now - float(_POSITIONS_PAYLOAD_CACHE.get("ts") or 0.0) < POSITIONS_CACHE_TTL_SECONDS
    ):
        return cached_payload

    if not POSITIONS_SCRIPT.exists():
        raise HTTPException(
            status_code=500,
            detail=f"positions.py not found at {POSITIONS_SCRIPT}"
        )

    cmd = [sys.executable, str(POSITIONS_SCRIPT)]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise HTTPException(
            status_code=500,
            detail=f"positions.py timed out after {SUBPROCESS_TIMEOUT_SECONDS}s"
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"positions.py execution failed: {e}"
        ) from e

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(
                f"positions.py failed with returncode={result.returncode}; "
                f"stdout={stdout.strip()} ; stderr={stderr.strip()}"
            )
        )

    try:
        payload = json.loads(stdout)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"positions.py returned invalid JSON: {e}"
        ) from e

    if isinstance(payload, dict):
        if payload.get("ok") is False:
            raise HTTPException(
                status_code=500,
                detail=f"positions.py returned error: {payload.get('error', 'unknown error')}"
            )

        positions = payload.get("positions")
        if positions is None:
            positions = []

        if not isinstance(positions, list):
            raise HTTPException(
                status_code=500,
                detail="positions.py returned invalid positions payload"
            )

        parsed = {
            "positions": [p for p in positions if isinstance(p, dict)],
            "balances": payload.get("balances") if isinstance(payload.get("balances"), dict) else {},
        }
        _POSITIONS_PAYLOAD_CACHE.update({"ts": time.time(), "data": parsed})
        return parsed

    if isinstance(payload, list):
        parsed = {
            "positions": [p for p in payload if isinstance(p, dict)],
            "balances": {},
        }
        _POSITIONS_PAYLOAD_CACHE.update({"ts": time.time(), "data": parsed})
        return parsed

    raise HTTPException(
        status_code=500,
        detail="positions.py must return a JSON object or JSON list"
    )


def build_position_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    symbol = str(row.get("symbol", "")).upper().strip()
    if not symbol:
        return None

    qty = safe_float(row.get("qty"), 0.0) or 0.0
    avg_cost = safe_float(row.get("avg_cost"), 0.0) or 0.0
    market_price = safe_float(row.get("market_price"))
    market_value = safe_float(row.get("market_value"))
    cost_basis = safe_float(row.get("cost_basis"))

    supplied_week52_high = safe_float(row.get("week52_high"))
    supplied_week52_low = safe_float(row.get("week52_low"))

    q = {
        "last_price": market_price if market_price is not None else 0.0,
        "close_price": market_price if market_price is not None else 0.0,
        "week52_high": supplied_week52_high,
        "week52_low": supplied_week52_low,
    }

    if market_price is None or market_value is None:
        try:
            live_q = get_quote_snapshot(symbol)
            q["last_price"] = safe_float(live_q.get("last_price"), q["last_price"]) or 0.0
            q["close_price"] = safe_float(live_q.get("close_price"), q["close_price"]) or 0.0

            if q["week52_high"] is None:
                q["week52_high"] = safe_float(live_q.get("week52_high"))
            if q["week52_low"] is None:
                q["week52_low"] = safe_float(live_q.get("week52_low"))
        except HTTPException:
            pass

    if market_price is None:
        market_price = safe_float(q.get("last_price"), 0.0) or 0.0

    if cost_basis is None:
        cost_basis = qty * avg_cost

    if market_value is None:
        market_value = qty * market_price

    gain_loss = market_value - cost_basis
    gain_loss_pct = ((market_value / cost_basis) - 1.0) * 100.0 if cost_basis > 0 else None

    week52_high = safe_float(q.get("week52_high"))
    week52_low = safe_float(q.get("week52_low"))

    dist_to_52w_high_dollar = (week52_high - market_price) if week52_high is not None else None
    dist_to_52w_high_pct = ((week52_high / market_price) - 1.0) * 100.0 if (week52_high and market_price) else None

    dist_from_52w_low_dollar = (market_price - week52_low) if week52_low is not None else None
    dist_from_52w_low_pct = ((market_price / week52_low) - 1.0) * 100.0 if (week52_low and market_price) else None

    return {
        "symbol": symbol,
        "qty": round(qty, 6),
        "avg_cost": round(avg_cost, 4),
        "market_price": round(market_price, 4),
        "market_value": round(market_value, 2),
        "cost_basis": round(cost_basis, 2),
        "gain_loss": round(gain_loss, 2),
        "gain_loss_pct": round(gain_loss_pct, 4) if gain_loss_pct is not None else None,
        "week52_high": round(week52_high, 4) if week52_high is not None else None,
        "week52_low": round(week52_low, 4) if week52_low is not None else None,
        "dist_to_52w_high_dollar": round(dist_to_52w_high_dollar, 4) if dist_to_52w_high_dollar is not None else None,
        "dist_to_52w_high_pct": round(dist_to_52w_high_pct, 4) if dist_to_52w_high_pct is not None else None,
        "dist_from_52w_low_dollar": round(dist_from_52w_low_dollar, 4) if dist_from_52w_low_dollar is not None else None,
        "dist_from_52w_low_pct": round(dist_from_52w_low_pct, 4) if dist_from_52w_low_pct is not None else None,
    }


# ============================================================
# Broker subprocess integration
# ============================================================

def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", s or "")


def _extract_final_json_object(stdout: str) -> dict:
    text = _strip_ansi(stdout or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                if isinstance(payload, dict):
                    return payload
            except Exception:
                pass

    decoder = json.JSONDecoder()
    starts = [i for i, ch in enumerate(text) if ch == "{"]

    last_error = None
    for i in starts:
        try:
            obj, end = decoder.raw_decode(text[i:])
            trailing = text[i + end:].strip()
            if trailing == "" and isinstance(obj, dict):
                return obj
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(
        f"Could not extract final JSON object from Trade.py output; "
        f"last_error={last_error}; stdout={text[:2000]!r}"
    )


def ensure_trade_subprocess_succeeded(result: subprocess.CompletedProcess[str]) -> dict:
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if result.returncode != 0:
        raise RuntimeError(
            f"Trade.py failed with returncode={result.returncode}; "
            f"stdout={stdout.strip()} ; stderr={stderr.strip()}"
        )

    order_id = None
    extracted_payload = None

    try:
        extracted_payload = _extract_final_json_object(stdout)
        if isinstance(extracted_payload, dict):
            order_id = extracted_payload.get("order_id")
    except Exception:
        extracted_payload = None

    return {
        "mode": "live",
        "trade_script": str(TRADE_SCRIPT),
        "returncode": result.returncode,
        "order_id": order_id,
        "trade_payload": extracted_payload,
        "stdout": stdout,
        "stderr": stderr,
    }


def broker_place_market_order(symbol: str, qty: int, side: str, acct: str) -> dict:
    cmd = [
        sys.executable,
        str(TRADE_SCRIPT),
        symbol,
        "--qty", str(qty),
        "--side", side,
        "--type", "MARKET",
        "--acct", acct,
    ]

    try:
        result = subprocess.run(
           cmd,
           stdout=subprocess.PIPE,
           stderr=subprocess.DEVNULL,   # 🔥 suppress noise
           text=True,
           timeout=15,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Trade.py timed out after {SUBPROCESS_TIMEOUT_SECONDS}s") from e
    except Exception as e:
        raise RuntimeError(f"Trade.py execution failed: {e}") from e

    return ensure_trade_subprocess_succeeded(result)


def broker_get_order_by_id(order_id: str, acct: str) -> dict:
    cmd = [
        sys.executable,
        str(TRADE_SCRIPT),
        "--get-order",
        "--order-id", str(order_id),
        "--acct", acct,
    ]

    logger.info(
        "Fetching broker order order_id=%s acct=%s script=%s",
        order_id, acct, TRADE_SCRIPT
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Trade.py lookup timed out after {SUBPROCESS_TIMEOUT_SECONDS}s") from e
    except Exception as e:
        raise RuntimeError(f"Trade.py lookup execution failed: {e}") from e

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if result.returncode != 0:
        raise RuntimeError(
            f"Trade.py lookup failed with returncode={result.returncode}; "
            f"stdout={stdout.strip()} ; stderr={stderr.strip()}"
        )

    payload = _extract_final_json_object(stdout)

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Trade.py lookup returned non-object JSON; stdout={stdout.strip()}"
        )

    return payload


# ============================================================
# Middleware
# ============================================================

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    try:
        response = await call_next(request)
        elapsed_ms = int((time.time() - start) * 1000)
        logger.info(
            "%s %s -> %s in %sms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
    except Exception:
        elapsed_ms = int((time.time() - start) * 1000)
        logger.exception(
            "%s %s -> exception in %sms",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise


# ============================================================
# Routes
# ============================================================

@app.get("/health")
def health() -> dict:
    require_server_configuration()
    cfg = load_risk_config()
    logger.info(
        "HEALTH ok trade_script_exists=%s positions_script_exists=%s pending_previews=%s",
        TRADE_SCRIPT.exists(),
        POSITIONS_SCRIPT.exists(),
        len(PENDING_PREVIEWS),
    )

    return {
        "ok": True,
        "service": "trade_server",
        "server_healthy": True,
        "winerror64_accept_disconnects_treated_as_transient": os.name == "nt",
        "trade_script_exists": TRADE_SCRIPT.exists(),
        "positions_script_exists": POSITIONS_SCRIPT.exists(),
        "risk_config_path": str(RISK_CONFIG_PATH),
        "risk_config_loaded": bool(cfg),
        "risk_config_last_load_ts": _RISK_CONFIG_LAST_LOAD_TS,
        "pending_previews": len(PENDING_PREVIEWS),
        "allowed_symbols": sorted(ALLOWED_SYMBOLS),
        "default_acct": DEFAULT_ACCT,
        "app_key_present": bool(os.getenv("app_key", "").strip()),
        "app_secret_present": bool(os.getenv("app_secret", "").strip()),
        "trade_api_key_present": bool(TRADE_API_KEY),
        "max_qty_per_order": cfg.get("MAX_QTY_PER_ORDER"),
        "max_notional_per_order": cfg.get("MAX_NOTIONAL_PER_ORDER"),
        "price_guards": cfg.get("PRICE_GUARDS"),
        "max_notional_by_symbol": cfg.get("MAX_NOTIONAL_BY_SYMBOL"),
        "block_outside_market_hours": cfg.get("BLOCK_OUTSIDE_MARKET_HOURS"),
        "allow_after_hours_with_fallback_price": cfg.get("ALLOW_AFTER_HOURS_WITH_FALLBACK_PRICE"),
        "require_live_quote_during_market_hours": cfg.get("REQUIRE_LIVE_QUOTE_DURING_MARKET_HOURS"),
        "require_live_quote_after_hours": cfg.get("REQUIRE_LIVE_QUOTE_AFTER_HOURS"),
    }


@app.get("/positions", response_model=PositionsResponse)
@app.get("/api/positions", response_model=PositionsResponse)

def get_positions(
    symbol: Optional[str] = Query(default=None),
    refresh: bool = Query(default=False),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
) -> PositionsResponse:
    require_api_key(x_api_key)

    requested_symbol = symbol.upper().strip() if symbol else None

    payload = run_positions_script(force_refresh=refresh)
    rows = payload.get("positions", [])
    balances = payload.get("balances", {}) or {}

    positions: List[Dict[str, Any]] = []

    for row in rows:
        built = build_position_row(row)
        if not built:
            continue
        if requested_symbol and built["symbol"] != requested_symbol:
            continue
        positions.append(built)

    positions.sort(key=lambda x: x["market_value"], reverse=True)

    total_market_value = round(sum(p["market_value"] for p in positions), 2)
    total_cost_basis = round(sum(p["cost_basis"] for p in positions), 2)
    total_gain_loss = round(total_market_value - total_cost_basis, 2)
    total_gain_loss_pct = (
        round(((total_market_value / total_cost_basis) - 1.0) * 100.0, 4)
        if total_cost_basis > 0
        else None
    )

    asset_total = safe_round(
        safe_float(balances.get("asset_total"), total_market_value),
        2,
    ) or total_market_value

    cash_available = safe_round(safe_float(balances.get("cash_available")), 2)
    settled_cash = safe_round(safe_float(balances.get("settled_cash")), 2)
    buying_power = safe_round(safe_float(balances.get("buying_power")), 2)
    
    preview_pending = calc_pending_buy_notional()

    orders = get_open_orders() or []
    broker_pending = calc_broker_pending_buy_notional(orders)

    pending_buy_notional = round(
       preview_pending + broker_pending,
       2
    )

    free_cash_after_pending = None
    if cash_available is not None:
        free_cash_after_pending = round(
           max(0.0, cash_available - pending_buy_notional), 2
    )

    total_account_value = safe_round(
        safe_float(
            balances.get("total_account_value"),
            asset_total + (cash_available or 0.0),
        ),
        2,
    )
    
    logger.info(
        "preview_pending=%s broker_pending=%s open_order_count=%s",
        preview_pending,
        broker_pending,
        len(orders),
    )
    return PositionsResponse(
        ok=True,
        count=len(positions),
        positions=[PositionRow(**p) for p in positions],
        summary=PositionsSummary(
            market_value=total_market_value,
            cost_basis=total_cost_basis,
            gain_loss=total_gain_loss,
            gain_loss_pct=total_gain_loss_pct,
            ),
        asset_total=asset_total,
        cash_available=cash_available,
        settled_cash=settled_cash,
        buying_power=buying_power,
        total_account_value=total_account_value,
        pending_buy_notional=pending_buy_notional,
        free_cash_after_pending=free_cash_after_pending,
    )


@app.get("/v1/orders/{order_id}", response_model=OrderLookupResponse)
def get_order_by_id(
    order_id: str,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
    acct: Optional[str] = None,
) -> OrderLookupResponse:
    require_api_key(x_api_key)

    order_id = (order_id or "").strip()
    acct = (acct or DEFAULT_ACCT).strip() or DEFAULT_ACCT

    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order_id")

    try:
        lookup_payload = broker_get_order_by_id(order_id=order_id, acct=acct)
    except Exception as e:
        logger.exception("ORDER LOOKUP failed order_id=%s acct=%s", order_id, acct)
        raise HTTPException(status_code=500, detail=f"Broker order lookup failed: {e}")

    summary = lookup_payload.get("summary") or {}
    result_block = lookup_payload.get("result") or {}
    result_data = result_block.get("data") or {}

    logger.info(
        "ORDER LOOKUP success order_id=%s acct=%s status=%s symbol=%s",
        order_id,
        acct,
        summary.get("status"),
        summary.get("symbol"),
    )

    return OrderLookupResponse(
        ok=True,
        order_id=order_id,
        acct=acct,
        broker_result=result_data,
        summary=summary,
    )


@app.get("/v1/orders/{order_id}/status", response_model=OrderStatusResponse)
def get_order_status(
    order_id: str,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
    acct: Optional[str] = None,
) -> OrderStatusResponse:
    require_api_key(x_api_key)

    order_id = (order_id or "").strip()
    acct = (acct or DEFAULT_ACCT).strip() or DEFAULT_ACCT

    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order_id")

    try:
        lookup_payload = broker_get_order_by_id(order_id=order_id, acct=acct)
    except Exception as e:
        logger.exception("ORDER STATUS failed order_id=%s acct=%s", order_id, acct)
        raise HTTPException(status_code=500, detail=f"Broker order status failed: {e}")

    summary = lookup_payload.get("summary") or {}
    status = summary.get("status")
    filled = bool(status == "FILLED" or (summary.get("filled_qty") or 0) > 0)

    logger.info(
        "ORDER STATUS success order_id=%s acct=%s status=%s symbol=%s filled=%s",
        order_id,
        acct,
        status,
        summary.get("symbol"),
        filled,
    )

    return OrderStatusResponse(
        ok=True,
        order_id=order_id,
        acct=acct,
        symbol=summary.get("symbol"),
        side=summary.get("side"),
        status=status,
        filled=filled,
        filled_qty=summary.get("filled_qty"),
        fill_price=summary.get("fill_price"),
        entered_time=summary.get("entered_time"),
        close_time=summary.get("close_time"),
    )


@app.post("/v1/orders/preview", response_model=PreviewResponse)
def preview_order(
    req: PreviewRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> PreviewResponse:
    require_api_key(x_api_key)
    cleanup_expired_previews()
    load_risk_config()

    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Missing Idempotency-Key")

    symbol = req.symbol
    side = req.side
    qty = req.qty

    require_allowed_symbol(symbol)
    require_safe_qty_hard_limit(qty)

    idem_key = preview_idempotency_key(idempotency_key)
    existing = PREVIEW_IDEMPOTENCY.get(idem_key)
    if existing:
        same_req = (
            existing["symbol"] == symbol
            and existing["side"] == side
            and existing["qty"] == qty
        )
        if not same_req:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key already used for different preview inputs"
            )
        return existing["response"]

    preview_id = uuid4().hex
    confirm_code = make_confirm_code()
    now = time.time()
    expires_at = now + PREVIEW_TTL_SECONDS

    PENDING_PREVIEWS[preview_id] = {
        "preview_id": preview_id,
        "confirm_code": confirm_code,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "created_at": now,
        "expires_at": expires_at,
        "confirmed": False,
        "consumed": False,
        "preview_idempotency_key": idempotency_key,
        "acct": DEFAULT_ACCT,
    }

    response = PreviewResponse(
        ok=True,
        preview_id=preview_id,
        confirm_code=confirm_code,
        symbol=symbol,
        side=side,
        qty=qty,
        expires_in_sec=PREVIEW_TTL_SECONDS,
        acct=DEFAULT_ACCT,
    )

    PREVIEW_IDEMPOTENCY[idem_key] = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "response": response,
    }

    logger.info(
        "PREVIEW created symbol=%s side=%s qty=%s preview_id=%s acct=%s idem=%s",
        symbol, side, qty, preview_id, DEFAULT_ACCT, idempotency_key
    )

    return response


@app.post("/v1/orders/confirm", response_model=ConfirmResponse)
def confirm_order(
    req: ConfirmRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> ConfirmResponse:
    require_api_key(x_api_key)
    cleanup_expired_previews()

    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Missing Idempotency-Key")

    idem_key = confirm_idempotency_key(idempotency_key)
    existing = CONFIRM_IDEMPOTENCY.get(idem_key)
    if existing:
        if existing["preview_id"] != req.preview_id:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key already used for a different preview_id"
            )
        return existing["response"]

    payload = PENDING_PREVIEWS.get(req.preview_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Preview not found or expired")

    if payload["consumed"]:
        raise HTTPException(status_code=409, detail="Preview already consumed")

    if payload["confirmed"]:
        raise HTTPException(status_code=409, detail="Preview already confirmed")

    if payload["confirm_code"] != req.confirm_code.strip():
        raise HTTPException(status_code=400, detail="Invalid confirm_code")

    if payload["expires_at"] < time.time():
        PENDING_PREVIEWS.pop(req.preview_id, None)
        raise HTTPException(status_code=410, detail="Preview expired")

    symbol = payload["symbol"]
    qty = payload["qty"]
    side = payload["side"]
    acct = payload["acct"]

    require_allowed_symbol(symbol)
    require_safe_qty_hard_limit(qty)

    cfg = load_risk_config()
    enforce_market_hours(cfg)
    enforce_qty_guard(symbol, qty, cfg)

    est_price, price_source = resolve_estimated_price(symbol, cfg)
    price_limit = enforce_price_guard(symbol, est_price, cfg)
    est_notional, max_notional = enforce_notional_guard(symbol, qty, est_price, cfg)

    logger.info(
        "RISK CHECK passed symbol=%s qty=%s est_price=%.2f price_source=%s price_limit=%.2f est_notional=%.2f max_notional=%.2f",
        symbol,
        qty,
        est_price,
        price_source,
        price_limit,
        est_notional,
        max_notional,
    )

    try:
        broker_result = broker_place_market_order(
            symbol=symbol,
            qty=qty,
            side=side,
            acct=acct,
        )
    except Exception as e:
        logger.exception(
            "CONFIRM failed symbol=%s side=%s qty=%s preview_id=%s idem=%s",
            symbol, side, qty, req.preview_id, idempotency_key
        )
        raise HTTPException(status_code=500, detail=f"Broker order failed: {e}")

    payload["confirmed"] = True
    payload["consumed"] = True
    _OPEN_ORDERS_CACHE.update({"ts": 0.0, "data": None})

    response = ConfirmResponse(
        ok=True,
        preview_id=req.preview_id,
        symbol=symbol,
        side=side,
        qty=qty,
        status="submitted",
        broker_result={
            "message": "order submitted",
            "order_id": broker_result.get("order_id"),
            "risk_est_price": est_price,
            "risk_price_source": price_source,
            "risk_price_limit": price_limit,
            "risk_est_notional": est_notional,
            "risk_max_notional": max_notional,
        },
    )

    CONFIRM_IDEMPOTENCY[idem_key] = {
        "preview_id": req.preview_id,
        "response": response,
    }

    logger.info(
        "CONFIRM success symbol=%s side=%s qty=%s preview_id=%s idem=%s order_id=%s",
        symbol,
        side,
        qty,
        req.preview_id,
        idempotency_key,
        broker_result.get("order_id"),
    )

    return response


# ============================================================
# BuyLow Log API for iPhone UI
# ============================================================

LOG_DIR = Path(os.getenv("BUYLOW_LOG_DIR", r"C:\temp\logs_ira1"))
BUYLOW_LOCK_DIR = Path(os.getenv("BUYLOW_LOCK_DIR", str(LOCKS_DIR)))
BUYLOW_LOCK_STALE_SEC = float(os.getenv("BUYLOW_LOCK_STALE_SEC", "60"))
BUYLOW_LOCK_RETRY_COUNT = int(os.getenv("BUYLOW_LOCK_RETRY_COUNT", "5"))
BUYLOW_LOCK_RETRY_SLEEP_SEC = float(os.getenv("BUYLOW_LOCK_RETRY_SLEEP_SEC", "0.2"))
_BUYLOW_STATUS_CACHE: dict[str, dict] = {}


class BuyLowLogLockBusy(RuntimeError):
    pass


def _buylow_cache_key(symbol: Optional[str]) -> str:
    return (symbol or "").strip().upper() or "__ALL__"


def _cache_buylow_summary(symbol: Optional[str], payload: dict) -> None:
    _BUYLOW_STATUS_CACHE[_buylow_cache_key(symbol)] = {
        "ts": time.time(),
        "payload": json.loads(json.dumps(payload, default=str)),
    }


def _cached_buylow_summary(symbol: Optional[str]) -> Optional[dict]:
    cached = _BUYLOW_STATUS_CACHE.get(_buylow_cache_key(symbol))
    if not cached:
        return None
    payload = json.loads(json.dumps(cached.get("payload") or {}, default=str))
    payload["cached"] = True
    payload["stale"] = True
    payload["cache_age_sec"] = round(time.time() - float(cached.get("ts") or 0.0), 2)
    payload["reason"] = "lock busy, showing cached status"
    summary = payload.get("summary")
    if isinstance(summary, dict):
        summary["reason"] = "lock busy, showing cached status"
    return payload


def _lock_paths_for_symbol(symbol: Optional[str]) -> list[Path]:
    paths = [BUYLOW_LOCK_DIR / "BUYLOW_PORTFOLIO.lock"]
    sym = (symbol or "").strip().upper()
    if sym:
        paths.insert(0, BUYLOW_LOCK_DIR / f"{sym}.lock")
    return paths


def _lock_paths_from_text(text: str) -> list[Path]:
    paths: list[Path] = []
    for match in re.finditer(r"Could not obtain lock:\s*([^\r\n]+?\.lock)", text or "", flags=re.IGNORECASE):
        raw = match.group(1).strip().strip("'\". ")
        if raw:
            paths.append(Path(raw))
    return paths


def _remove_stale_lock(lock_path: Path) -> bool:
    try:
        if not lock_path.exists() or not lock_path.is_file():
            return False
        age_sec = time.time() - lock_path.stat().st_mtime
        if age_sec <= BUYLOW_LOCK_STALE_SEC:
            return False
        lock_path.unlink()
        logger.warning("[LOCK] removed stale lock %s age_sec=%.1f", lock_path, age_sec)
        return True
    except FileNotFoundError:
        return False
    except Exception as exc:
        logger.warning("[LOCK] could not remove stale lock %s: %s", lock_path, exc)
        return False


def _remove_stale_locks(paths: list[Path]) -> bool:
    removed = False
    seen: set[str] = set()
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        removed = _remove_stale_lock(path) or removed
    return removed


def _is_lock_busy_summary(summary: dict) -> bool:
    fields = [
        summary.get("display_text"),
        summary.get("warn"),
        summary.get("why"),
        summary.get("skip"),
        summary.get("hold"),
    ]
    text = " ".join(str(v) for v in fields if v)
    return "Could not obtain lock" in text


def _is_lock_warning(line: str) -> bool:
    return "Could not obtain lock" in (line or "")


def _latest_buylow_log_file() -> Optional[Path]:
    """Return the newest BuyLow log file."""
    patterns = ["buylow_*.log", "*.log"]
    seen: set[Path] = set()
    files: list[Path] = []

    for pattern in patterns:
        for f in LOG_DIR.glob(pattern):
            if f.is_file() and f not in seen:
                seen.add(f)
                files.append(f)

    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _tail_text(path: Path, max_chars: int = 30000) -> str:
    """Read only the tail of a log file for mobile-friendly responses."""
    try:
        txt = path.read_text(encoding="utf-8", errors="ignore")
    except UnicodeDecodeError:
        txt = path.read_text(encoding="utf-8-sig", errors="ignore")
    return txt[-max_chars:]


def _pick_last(lines: list[str], prefixes: list[str]) -> str:
    for line in reversed(lines):
        s = line.strip()
        if any(s.startswith(p) for p in prefixes):
            return s
    return ""


def _detect_log_status(lines: list[str]) -> str:
    for line in reversed(lines):
        s = line.strip()

        if "[TRIGGER BUY]" in s or "Order submitted" in s or s.startswith("[PREVIEW]") or s.startswith("[PREVIEW-TRIGGER]"):
            return "BUY"
        if s.startswith("[READY]") or " -> BUY" in s:
            return "BUY_SIGNAL"
        if s.startswith("[CAP]"):
            return "CAP"
        if s.startswith("[CAP-DETAIL]"):
            return "CAP"
        if s.startswith("[SPREAD-BLOCK]"):
            return "SPREAD_BLOCK"
        if s.startswith("[OK]"):
            return "SPREAD"
        if s.startswith("[HOLD]"):
            return "HOLD"
        if s.startswith("[SKIP]"):
            return "SKIP"
        if s.startswith("[WHY]") or s.startswith("[PASS]") or " -> wait" in s.lower():
            return "WAIT"
        if s.startswith("[WARN]") and _is_lock_warning(s):
            return "LOCK_BUSY"
        if s.startswith("[WARN]"):
            return "WARN"
        if " -> hold" in s:
            return "HOLD"

    return "UNKNOWN"


def _detect_log_symbol(lines: list[str]) -> str:
    known = ["SPY", "QQQ", "GLD", "COST", "DIA", "NVDA", "AMZN", "DTE", "IBIT", "EETH", "TQQQ"]
    for line in reversed(lines):
        s = line.strip()
        for sym in known:
            if s.startswith(sym + " "):
                return sym
            if f" {sym} " in f" {s} ":
                return sym
    return ""


def _line_mentions_symbol(line: str, symbol: str) -> bool:
    """Return True when a log line belongs to the requested symbol."""
    if not symbol:
        return True

    sym = symbol.strip().upper()
    s = (line or "").upper()

    # Common BuyLow formats:
    #   QQQ ask=...
    #   [HOLD] QQQ strict-atr: ...
    #   [SKIP] QQQ ...
    #   [WARN] QQQ: ...
    prefixes = [
        "HOLD", "SKIP", "WARN", "CAP", "CAP-DETAIL", "OK",
        "SPREAD-BLOCK", "WHY", "PASS", "READY", "WAIT",
        "TRIGGER", "TRIGGER BUY", "PREVIEW", "PREVIEW-TRIGGER",
    ]
    patterns = [rf"^\s*{re.escape(sym)}\b"]
    patterns.extend(rf"\[{re.escape(prefix)}\]\s+{re.escape(sym)}\b" for prefix in prefixes)
    return any(re.search(pat, s) for pat in patterns)


def _filter_lines_for_symbol(lines: list[str], symbol: Optional[str]) -> list[str]:
    if not symbol:
        return lines
    sym = symbol.strip().upper()
    return [line for line in lines if _line_mentions_symbol(line, sym)]


def _is_valid_buylow_status_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if _is_lock_warning(s):
        return False
    lower = s.lower()
    prefixes = (
        "[HOLD]", "[SKIP]", "[WHY]", "[SPREAD-BLOCK]", "[OK]",
        "[CAP]", "[CAP-DETAIL]", "[PASS]", "[READY]", "[WAIT]",
        "[TRIGGER]", "[TRIGGER BUY]", "[PREVIEW]", "[PREVIEW-TRIGGER]",
    )
    return (
        s.startswith(prefixes)
        or " -> hold" in lower
        or " -> buy" in lower
        or " -> wait" in lower
        or " ask=" in lower
        or lower.startswith("ask=")
    )


def _count_symbol_debug_lines(all_lines: list[str], symbol: Optional[str]) -> dict[str, int]:
    if not symbol:
        lock_warn_count = sum(1 for line in all_lines if line.strip().startswith("[WARN]") and _is_lock_warning(line))
        return {
            "ignored_warn_count": 0,
            "ignored_lock_warn_count": lock_warn_count,
            "matched_symbol_line_count": sum(1 for line in all_lines if _is_valid_buylow_status_line(line)),
        }

    ignored_warn_count = 0
    ignored_lock_warn_count = 0
    matched_symbol_line_count = 0
    for line in all_lines:
        s = (line or "").strip()
        if not s:
            continue
        matches_symbol = _line_mentions_symbol(s, symbol)
        if s.startswith("[WARN]"):
            if _is_lock_warning(s):
                ignored_lock_warn_count += 1
                continue
            if not matches_symbol:
                ignored_warn_count += 1
                continue
        if matches_symbol and _is_valid_buylow_status_line(s):
            matched_symbol_line_count += 1

    return {
        "ignored_warn_count": ignored_warn_count,
        "ignored_lock_warn_count": ignored_lock_warn_count,
        "matched_symbol_line_count": matched_symbol_line_count,
    }


def _clean_buylow_hold(line: str, symbol: str = "") -> str:
    """Return a compact one-line reason for the iPhone card.

    Examples:
      [HOLD] QQQ strict-atr: ask 660.99 > target 625.36
        -> 660.99→625.36 (-5.4%)
      QQQ ask=660.99 target=625.36 -> hold
        -> 660.99→625.36 (-5.4%)
    """
    s = (line or "").strip()
    if not s:
        return ""

    sym = (symbol or "").strip().upper()

    # Remove noisy prefixes while preserving the decision reason.
    s = re.sub(r"^\[HOLD\]\s*", "", s, flags=re.IGNORECASE)
    if sym:
        s = re.sub(rf"^{re.escape(sym)}\s*", "", s, flags=re.IGNORECASE)

    s = s.replace("strict-atr:", "")

    # Format examples:
    #   ask 658.33 > target 625.36  ->  658.33→625.36 (-5.0%)
    #   ask=658.33 target=625.36 -> hold  ->  658.33→625.36 (-5.0%)
    ask_match = re.search(r"ask[=\s]+([0-9.]+)", s, flags=re.IGNORECASE)
    target_match = re.search(r"target[=\s]+([0-9.]+)", s, flags=re.IGNORECASE)
    if ask_match and target_match:
        ask = safe_float(ask_match.group(1))
        target = safe_float(target_match.group(1))
        if ask and target and ask > 0:
            pct = ((target - ask) / ask) * 100.0
            s = f"{ask:.2f}→{target:.2f} ({pct:.1f}%)"
        else:
            s = f"{ask_match.group(1)}→{target_match.group(1)}"

    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > 52:
        s = s[:49].rstrip() + "…"
    return s


def _pick_last_hold_like(lines: list[str], symbol: Optional[str] = None) -> str:
    """Find the latest HOLD-style line for a symbol.

    Supports both formats produced by buylow_new.py:
      [HOLD] QQQ strict-atr: ask 658.33 > target 625.36.
      QQQ ask=... gate=max ... -> hold
    """
    sym = (symbol or "").strip().upper()
    for line in reversed(lines):
        s = (line or "").strip()
        if not s:
            continue
        if sym and not _line_mentions_symbol(s, sym):
            continue

        lower = s.lower()
        if "[hold]" in lower or "-> hold" in lower or " strict-atr:" in lower:
            return s
    return ""


def _simple_buylow_status(raw_status: str) -> str:
    """Map raw log status into the four iPhone statuses."""
    s = (raw_status or "").upper()
    if s in {"BUY", "BUY_SIGNAL", "TRIGGER", "TRIGGER_BUY", "PREVIEW"}:
        return "READY"
    if s in {"CAP", "SPREAD_BLOCK", "SPREAD", "SKIP", "BLOCKED"}:
        return "BLOCKED"
    if s in {"LOCK_BUSY", "PASS", "WAIT", "WHY", "UNKNOWN"}:
        return "WAIT"
    if s in {"WARN", "ERROR", "CHECK"}:
        return "CHECK"
    return "WAIT"


def _short_blocked_reason(summary_lines: list[str]) -> str:
    cap = _pick_last(summary_lines, ["[CAP]"])
    spread = _pick_last(summary_lines, ["[SPREAD-BLOCK]"])
    skip = _pick_last(summary_lines, ["[SKIP]"])
    raw = spread or cap or skip
    if not raw:
        return "Blocked"
    raw = re.sub(r"^\[(CAP|SPREAD-BLOCK|SKIP)\]\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+", " ", raw).strip(" .")
    if len(raw) > 44:
        raw = raw[:41].rstrip() + "…"
    return raw or "Blocked"


def _short_check_reason(summary_lines: list[str]) -> str:
    warn = _pick_last(summary_lines, ["[WARN]"])
    if not warn:
        return "Check logs"
    if "HTTPSConnectionPool" in warn or "Read timed out" in warn or "timed out" in warn.lower():
        return "API timeout"
    warn = re.sub(r"^\[WARN\]\s*", "", warn, flags=re.IGNORECASE)
    warn = re.sub(r"\s+", " ", warn).strip(" .")
    if len(warn) > 44:
        warn = warn[:41].rstrip() + "…"
    return warn or "Check logs"


def _extract_buylow_summary(content: str, symbol: Optional[str] = None) -> dict:
    all_lines = content.splitlines()
    sym = (symbol or "").strip().upper()
    debug_counts = _count_symbol_debug_lines(all_lines, sym)

    if sym:
        lines = [
            line for line in _filter_lines_for_symbol(all_lines, sym)
            if _is_valid_buylow_status_line(line)
        ]
    else:
        lines = [
            line for line in all_lines
            if _is_valid_buylow_status_line(line)
        ]

    hold_raw = _pick_last_hold_like(lines, sym)
    skip_raw = _pick_last(lines, ["[SKIP]"])
    warn_raw = ""
    signal_raw = _pick_last(lines, ["SPY ", "QQQ ", "GLD ", "NVDA ", "MSFT ", "AAPL ", "COST ", "TQQQ "])
    trigger_raw = _pick_last(lines, ["[TRIGGER]", "[TRIGGER BUY]", "[PREVIEW-TRIGGER]", "[PREVIEW]"])
    cap_raw = _pick_last(lines, ["[CAP]"])
    cap_detail_raw = _pick_last(lines, ["[CAP-DETAIL]"])
    why_raw = _pick_last(lines, ["[WHY]"])
    pass_raw = _pick_last(lines, ["[PASS]"])
    spread_raw = _pick_last(lines, ["[OK]", "[SPREAD-BLOCK]"])

    raw_status = _detect_log_status(lines) if lines else "UNKNOWN"
    if trigger_raw or raw_status in ("BUY", "BUY_SIGNAL"):
        raw_status = "BUY_SIGNAL"
    elif cap_raw or cap_detail_raw:
        raw_status = "CAP"
    elif spread_raw.startswith("[SPREAD-BLOCK]"):
        raw_status = "SPREAD_BLOCK"
    elif spread_raw:
        raw_status = "SPREAD"
    elif skip_raw:
        raw_status = "SKIP"
    elif hold_raw:
        raw_status = "HOLD"
    elif why_raw:
        raw_status = "WHY"
    elif pass_raw:
        raw_status = "PASS"

    simple_status = _simple_buylow_status(raw_status)
    detected_symbol = sym or _detect_log_symbol(lines)

    if sym and not lines:
        raw_status = "UNKNOWN"
        simple_status = "WAIT"
        display_text = f"No recent {sym} BuyLow status"
    elif simple_status == "READY":
        display_text = "BUY signal"
    elif simple_status == "BLOCKED":
        display_text = _short_blocked_reason(lines)
    elif simple_status == "CHECK":
        display_text = _short_check_reason(lines)
    else:
        display_text = (
            _clean_buylow_hold(hold_raw, detected_symbol)
            or why_raw
            or pass_raw
            or "No signal yet"
        )

    return {
        "status": simple_status,
        "raw_status": raw_status,
        "symbol": detected_symbol,
        "display_text": display_text,
        "pass_line": pass_raw,
        "account": _pick_last(all_lines, ["[ACCT]"]),
        "brake": _pick_last(all_lines, ["[BRAKE]"]),
        "cap": cap_raw,
        "cap_detail": cap_detail_raw,
        "why": why_raw,
        "spread": spread_raw,
        "hold": hold_raw,
        "hold_text": display_text,
        "skip": skip_raw,
        "warn": warn_raw,
        "trigger": trigger_raw,
        "signal": signal_raw,
        "ignored_warn_count": debug_counts["ignored_warn_count"],
        "ignored_lock_warn_count": debug_counts["ignored_lock_warn_count"],
        "matched_symbol_line_count": debug_counts["matched_symbol_line_count"],
    }


@app.get("/api/logs/latest")
def api_logs_latest(
    max_chars: int = Query(default=30000, ge=1000, le=200000),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
):
    require_api_key(x_api_key)

    f = _latest_buylow_log_file()
    if not f:
        return {"ok": False, "error": f"no BuyLow logs found in {LOG_DIR}"}

    content = _tail_text(f, max_chars=max_chars)
    return {
        "ok": True,
        "file": f.name,
        "path": str(f),
        "content": content,
    }


@app.get("/api/logs/summary")
def api_logs_summary(
    symbol: Optional[str] = Query(default=None),
    max_chars: int = Query(default=30000, ge=1000, le=200000),
    search_files: int = Query(default=30, ge=1, le=100),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
):
    require_api_key(x_api_key)

    sym = symbol.strip().upper() if symbol else None

    for attempt in range(BUYLOW_LOCK_RETRY_COUNT):
        _remove_stale_locks(_lock_paths_for_symbol(sym))

        try:
            # Prefer real BuyLow run logs and ignore older helper/order logs that may
            # also live in the same folder. This prevents the iPhone card from showing
            # WAIT / No signal yet simply because the newest *.log was not a BuyLow log.
            files = sorted(
                [f for f in LOG_DIR.glob("buylow_*.log") if f.is_file() and f.stat().st_size > 0],
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )

            # Fallback only if the folder has no buylow_*.log files.
            if not files:
                files = sorted(
                    [f for f in LOG_DIR.glob("*.log") if f.is_file() and f.stat().st_size > 0],
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )

            if not files:
                return {"ok": False, "error": f"no BuyLow logs found in {LOG_DIR}"}

            # For per-symbol cards, search recent BuyLow logs and strongly prefer a
            # file that actually contains a HOLD/BUY/BLOCK/CHECK signal for that symbol.
            chosen_file = files[0]
            chosen_summary = None
            fallback_summary = None
            fallback_file = files[0]

            for f in files[:search_files]:
                content = _tail_text(f, max_chars=max_chars)
                if sym and sym not in content.upper():
                    continue

                _remove_stale_locks(_lock_paths_from_text(content))
                summary = _extract_buylow_summary(content, symbol=sym)

                # Keep the newest candidate as fallback, but do not stop on generic
                # WAIT / No signal yet. Continue searching for a real symbol line.
                if fallback_summary is None:
                    fallback_file = f
                    fallback_summary = summary

                if not sym:
                    if summary.get("raw_status") not in ("UNKNOWN", ""):
                        chosen_file = f
                        chosen_summary = summary
                        break
                else:
                    if int(summary.get("matched_symbol_line_count") or 0) > 0:
                        chosen_file = f
                        chosen_summary = summary
                        break

            if chosen_summary is None:
                if fallback_summary is not None:
                    chosen_file = fallback_file
                    chosen_summary = fallback_summary
                else:
                    content = _tail_text(chosen_file, max_chars=max_chars)
                    _remove_stale_locks(_lock_paths_from_text(content))
                    chosen_summary = _extract_buylow_summary(content, symbol=sym)

            if _is_lock_busy_summary(chosen_summary):
                _remove_stale_locks(
                    _lock_paths_for_symbol(sym)
                    + _lock_paths_from_text(" ".join(str(v) for v in chosen_summary.values() if v))
                )
                raise BuyLowLogLockBusy("Could not obtain lock")

            payload = {
                "ok": True,
                "file": chosen_file.name,
                "path": str(chosen_file),
                "symbol": sym or "",
                "summary": chosen_summary,
                "cached": False,
                "stale": False,
            }
            _cache_buylow_summary(sym, payload)
            return payload

        except BuyLowLogLockBusy:
            if attempt < BUYLOW_LOCK_RETRY_COUNT - 1:
                time.sleep(BUYLOW_LOCK_RETRY_SLEEP_SEC)
                continue

    cached = _cached_buylow_summary(sym)
    if cached is not None:
        return cached

    return {
        "ok": False,
        "symbol": sym or "",
        "cached": False,
        "stale": True,
        "reason": "lock busy, showing cached status",
        "error": "lock busy and no cached status available",
            "summary": {
                "status": "WAIT",
                "raw_status": "LOCK_BUSY",
                "symbol": sym or "",
                "display_text": "lock busy, no cached status available",
                "reason": "lock busy, showing cached status",
                "ignored_warn_count": 0,
                "ignored_lock_warn_count": 1,
                "matched_symbol_line_count": 0,
            },
        }


@app.get("/api/logs/search")
def api_logs_search(
    symbol: str = Query(..., min_length=1),
    max_files: int = Query(default=10, ge=1, le=50),
    max_chars: int = Query(default=3000, ge=500, le=50000),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
):
    require_api_key(x_api_key)

    sym = symbol.strip().upper()
    files = sorted(LOG_DIR.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)

    results = []
    for f in files[:max_files]:
        txt = _tail_text(f, max_chars=max(max_chars * 4, 12000))
        matching_lines = [line for line in txt.splitlines() if sym in line.upper()]
        if matching_lines:
            snippet = "\n".join(matching_lines[-40:])[-max_chars:]
            results.append({
                "file": f.name,
                "path": str(f),
                "summary": _extract_buylow_summary(txt, symbol=sym),
                "snippet": snippet,
            })

    return {"ok": True, "symbol": sym, "matches": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.trade_server:app", host="127.0.0.1", port=8080, reload=False)
