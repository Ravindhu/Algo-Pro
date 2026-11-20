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

from quant.research.costs import CostModel  # owner-confirmed statutory rate table (same as the paper estimate)

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
        "hold_days": None,
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


def _trade_seconds(item: Dict) -> Optional[float]:
    """Trade time as epoch seconds, or None when the tradebook row has no readable time."""
    for key in ("timestamp", "tradetime", "trade_time", "time", "date"):
        value = item.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            if math.isfinite(value):
                return float(value) / 1000 if value > 1e12 else float(value)
            continue
        text = str(value).strip()
        for parse in (lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
                      lambda t: datetime.strptime(t, "%d-%m-%Y %H:%M:%S"),
                      lambda t: datetime.strptime(t, "%d-%m-%Y")):
            try:
                moment = parse(text)
            except ValueError:
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=IST)
            return moment.timestamp()
    return None


def open_since(trades) -> Dict[tuple, float]:
    """Start time of each (symbol, exchange) position that is open now.

    Walks the trades in time order. The start is the last moment the net quantity
    went from flat to held, so a position closed and bought again counts from the rebuy.
    """
    rows = []
    for item in trades if isinstance(trades, list) else []:
        if not isinstance(item, dict):
            continue
        qty = _num(item.get("quantity"))
        when = _trade_seconds(item)
        action = str(item.get("action", "")).upper()
        if qty is None or when is None or action not in ("BUY", "SELL"):
            continue
        key = (str(item.get("symbol", "")), str(item.get("exchange", "")))
        rows.append((when, key, qty if action == "BUY" else -qty))
    rows.sort(key=lambda r: r[0])
    net: Dict[tuple, float] = {}
    start: Dict[tuple, float] = {}
    for when, key, signed in rows:
        before = net.get(key, 0.0)
        after = before + signed
        net[key] = after
        if before <= 0 < after:
            start[key] = when
        elif after <= 0:
            start.pop(key, None)
    return start


def _trade_price(item: Dict) -> Optional[float]:
    for key in ("average_price", "averageprice", "price", "tradeprice"):
        value = _num(item.get(key))
        if value is not None and value > 0:
            return value
    return None


def trade_costs(trades, model: Optional[CostModel] = None) -> Dict:
    """Estimated statutory cost of the day's broker fills, using the research CostModel.

    CNC fills are priced as delivery, MIS as intraday. Rows without a usable quantity,
    price or side are counted as skipped, never guessed.
    """
    model = model or CostModel()
    inr, fills, skipped = 0.0, 0, 0
    for item in trades if isinstance(trades, list) else []:
        qty = _num(item.get("quantity")) if isinstance(item, dict) else None
        price = _trade_price(item) if isinstance(item, dict) else None
        action = str(item.get("action", "")).upper() if isinstance(item, dict) else ""
        if qty is None or price is None or action not in ("BUY", "SELL"):
            skipped += 1
            continue
        product = "intraday" if str(item.get("product", "")).upper() == "MIS" else "delivery"
        inr += model.trade_cost(abs(qty) * price, action, product)
        fills += 1
    return {"inr": round(inr, 2), "fills": fills, "skipped": skipped,
            "basis": "estimated: research CostModel on today's broker fills"}


def hold_days(start_seconds: Optional[float], now_seconds: float) -> Optional[int]:
    """Calendar days from the start date to today, both in IST. None when the start is unknown."""
    if start_seconds is None:
        return None
    start_day = datetime.fromtimestamp(start_seconds, IST).date()
    today = datetime.fromtimestamp(now_seconds, IST).date()
    return max(0, (today - start_day).days)


class HoldingsReader:
    """Reads holdings at most once per CACHE_TTL_S. Errors are cached too, so a dead OpenAlgo is not hammered."""

    def __init__(self, fetch: Callable[[str], Dict], key_source: Callable[[], str],
                 clock: Callable[[], float], ttl_s: float = CACHE_TTL_S,
                 trades: Optional[Callable[[str], Dict]] = None) -> None:
        self._fetch = fetch
        self._trades = trades
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
                self._add_hold_days(key, result, now)
            except Exception as exc:  # name the error type only: never echo the payload or key
                result = _state("error", as_of, f"OpenAlgo unreachable ({type(exc).__name__})")
        self._cache, self._at = result, now
        return result

    def _add_hold_days(self, key: str, result: Dict, now: float) -> None:
        """Fill hold_days and today's estimated trade cost from the tradebook. Any failure leaves them blank."""
        if self._trades is None or result.get("state") != "broker_read":
            return
        try:
            reply = self._trades(key)
            trades = reply.get("data") if isinstance(reply, dict) and reply.get("status") == "success" else None
            if trades is None:
                result["trade_cost"] = None
                return
            starts = open_since(trades)
            costs = trade_costs(trades)
        except Exception:  # name nothing: the tradebook is optional
            result["trade_cost"] = None
            return
        for row in result["holdings"]:
            start = starts.get((row["symbol"], row["exchange"]))
            row["hold_days"] = hold_days(start, now)
        invested = (result.get("summary") or {}).get("invested")
        costs["pct_of_invested"] = round(costs["inr"] / invested * 100, 4) if invested else None
        result["trade_cost"] = costs
