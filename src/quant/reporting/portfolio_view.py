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


def delivery_gains(trades, min_days: float = 365.0) -> Dict:
    """Realised gains on delivery (CNC) sells, matched FIFO to earlier CNC buys in the same list.

    Holding period is sell time minus buy time. Held more than ``min_days`` is long-term.
    Intraday (MIS) fills are excluded (speculative income, not capital gains). A sell with
    no matching buy in the list is counted as unmatched and not taxed: the tradebook may
    not contain the buy, and the cost basis is then unknown.
    """
    lots: Dict[tuple, list] = {}
    out = {"short_term": 0.0, "long_term": 0.0, "matched_qty": 0, "unmatched_qty": 0,
           "intraday_excluded": 0}
    rows = []
    for item in trades if isinstance(trades, list) else []:
        if not isinstance(item, dict):
            continue
        qty = _num(item.get("quantity"))
        price = _trade_price(item)
        when = _trade_seconds(item)
        action = str(item.get("action", "")).upper()
        if qty is None or price is None or when is None or action not in ("BUY", "SELL"):
            continue
        rows.append((when, action, abs(qty), price, str(item.get("product", "")).upper(),
                     (item.get("symbol"), item.get("exchange"))))
    for when, action, qty, price, product, key in sorted(rows, key=lambda r: r[0]):
        if product != "CNC":
            out["intraday_excluded"] += 1
            continue
        book = lots.setdefault(key, [])
        if action == "BUY":
            book.append([qty, price, when])
            continue
        remaining = qty
        while remaining > 0 and book:
            take = min(book[0][0], remaining)
            gain = (price - book[0][1]) * take
            held_days = (when - book[0][2]) / 86400.0
            out["long_term" if held_days > min_days else "short_term"] += gain
            out["matched_qty"] += int(take)
            remaining -= take
            book[0][0] -= take
            if book[0][0] <= 0:
                book.pop(0)
        out["unmatched_qty"] += int(remaining)
    out["short_term"] = round(out["short_term"], 2)
    out["long_term"] = round(out["long_term"], 2)
    return out


def apply_margin(payload: Dict, borrowed_inr: float, model: Optional[CostModel] = None) -> Dict:
    """Add MTF % and today's MTF interest to a portfolio payload (real money only).

    MTF % = borrowed / holdings value x 100. The day's interest (12 % a year, daily base) is
    added to the cost of trading, which is then re-expressed as % of invested. With no
    tradebook the cost block is created with zero fills, because interest still accrues.
    """
    model = model or CostModel()
    summary = payload.get("summary") or {}
    value, invested = _num(summary.get("value")), _num(summary.get("invested"))
    borrowed = max(0.0, float(borrowed_inr or 0.0))
    interest = round(model.mtf_daily_interest(borrowed), 2)
    payload["mtf"] = {
        "borrowed_inr": round(borrowed, 2),
        "mtf_pct": round(borrowed / value * 100, 4) if value else None,
        "annual_rate_pct": model.mtf_annual_rate * 100,
        "daily_interest_inr": interest,
    }
    cost = payload.get("trade_cost")
    if cost is None and borrowed > 0:
        cost = {"inr": 0.0, "tax_inr": 0.0, "stcg_tax_inr": 0.0, "ltcg_tax_inr": 0.0,
                "total_inr": 0.0, "fills": 0, "skipped": 0, "realized": None,
                "basis": "no tradebook: interest only"}
    if cost is not None:
        cost["interest_inr"] = interest
        cost["total_inr"] = round(float(cost.get("total_inr") or 0.0) + interest, 2)
        cost["pct_of_invested"] = round(cost["total_inr"] / invested * 100, 4) if invested else None
        payload["trade_cost"] = cost
    return payload


def trade_costs(trades, model: Optional[CostModel] = None) -> Dict:
    """Estimated cost of the day's broker fills: statutory charges plus capital-gains tax.

    CNC fills are priced as delivery, MIS as intraday. Rows without a usable quantity,
    price or side are counted as skipped, never guessed. Tax applies only to matched
    delivery gains (see delivery_gains).
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
    gains = delivery_gains(trades, model.ltcg_min_days)
    tax = model.capital_gains_tax(gains["short_term"], gains["long_term"])
    total = round(inr + tax["tax_inr"], 2)
    return {"inr": round(inr, 2), "tax_inr": tax["tax_inr"], "stcg_tax_inr": tax["stcg_inr"],
            "ltcg_tax_inr": tax["ltcg_inr"], "total_inr": total, "fills": fills, "skipped": skipped,
            "realized": gains,
            "basis": "estimated: research CostModel on today's broker fills, plus STCG/LTCG tax on matched delivery gains"}


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
        costs["pct_of_invested"] = round(costs["total_inr"] / invested * 100, 4) if invested else None
        result["trade_cost"] = costs
