"""Portfolio header data: broker holdings read through OpenAlgo. Read-only, never sends orders.

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (read-only holdings; no order path)
Decision: docs/decisions/0032-readonly-holdings-openalgo.md

States: "broker_read" (OpenAlgo answered), "not_connected" (no API key), "error" (OpenAlgo failed).
Numbers are passed through as the broker reports them. Missing values stay None; nothing is estimated.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Callable, Dict, Optional
from zoneinfo import ZoneInfo

MAX_ROWS = 50          # memory cap for the table (AGENTS.md token rules)
CACHE_TTL_S = 30.0     # broker read at most every 30 s
IST = ZoneInfo("Asia/Kolkata")
SOURCE = "Broker holdings, read through OpenAlgo"


def _num(value) -> Optional[float]:
    """Finite float, or None. Bools and text are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _state(state: str, as_of: str, error: Optional[str]) -> Dict:
    """Empty portfolio with a state and reason. Used for every non-success case."""
    return {"state": state, "source": SOURCE, "as_of": as_of, "error": error,
            "summary": None, "holdings": [], "truncated": False}


def _row(item: Dict) -> Dict:
    """One holding, keeping only the fields the header shows."""
    return {
        "symbol": str(item.get("symbol", ""))[:30],
        "exchange": str(item.get("exchange", ""))[:10],
        "quantity": _num(item.get("quantity")),
        "average_price": _num(item.get("average_price")),
        "ltp": _num(item.get("ltp")),
        "pnl": _num(item.get("pnl")),
        "pnlpercent": _num(item.get("pnlpercent")),
    }


def build_portfolio(reply, as_of: str) -> Dict:
    """Shape one OpenAlgo holdings reply. Any reply that is not a clear success becomes an error state."""
    if not isinstance(reply, dict) or reply.get("status") != "success" or not isinstance(reply.get("data"), dict):
        message = reply.get("message") if isinstance(reply, dict) else None
        return _state("error", as_of, str(message or "OpenAlgo gave no holdings"))
    data = reply["data"]
    rows = [r for r in data.get("holdings", []) if isinstance(r, dict)] if isinstance(data.get("holdings"), list) else []
    stats = data.get("statistics") if isinstance(data.get("statistics"), dict) else {}
    return {
        "state": "broker_read",
        "source": SOURCE,
        "as_of": as_of,
        "error": None,
        "summary": {
            "value": _num(stats.get("totalholdingvalue")),
            "invested": _num(stats.get("totalinvvalue")),
            "pnl": _num(stats.get("totalprofitandloss")),
            "pnl_pct": _num(stats.get("totalpnlpercentage")),
            "count": len(rows),
        },
        "holdings": [_row(r) for r in rows[:MAX_ROWS]],
        "truncated": len(rows) > MAX_ROWS,
    }


class HoldingsReader:
    """Reads holdings at most once per CACHE_TTL_S. Errors are cached too, so a dead OpenAlgo is not hammered."""

    def __init__(self, fetch: Callable[[str], Dict], key_source: Callable[[], str],
                 clock: Callable[[], float], ttl_s: float = CACHE_TTL_S) -> None:
        self._fetch = fetch
        self._key_source = key_source
        self._clock = clock
        self._ttl = ttl_s
        self._cache: Optional[Dict] = None
        self._at = 0.0

    def read(self) -> Dict:
        now = self._clock()
        if self._cache is not None and now - self._at < self._ttl:
            return self._cache
        as_of = datetime.fromtimestamp(now, IST).isoformat(timespec="seconds")
        try:
            key = self._key_source()
        except Exception:  # message kept generic: never echo secrets
            result = _state("not_connected", as_of, "OPENALGO_API_KEY is not set in .env")
        else:
            try:
                result = build_portfolio(self._fetch(key), as_of)
            except Exception as exc:  # name the error type only: never echo the payload or key
                result = _state("error", as_of, f"OpenAlgo unreachable ({type(exc).__name__})")
        self._cache, self._at = result, now
        return result
