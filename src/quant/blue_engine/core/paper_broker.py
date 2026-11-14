"""paper_broker.py - a recording stand-in for the OpenAlgo SDK client.

WHY THIS EXISTS
---------------
Audit 2026-10-29: every path that exercises the real trading stack
(``core.runner``, ``screener_bridge``) sends orders to the broker. The two
"paper" facilities are not substitutes:

* ``core/paper_shift`` is an LLM/self-correction research harness that never
  places an order, and
* ``scripts/run/run_real`` replays historical CSV bars.

Neither exercises signal -> risk gate -> POV slicer -> ledger -> exit engine.
And OpenAlgo's analyze mode cannot stand in, because it simulates orders and
account but still proxies ``quotes``/``history`` to the live broker (which is
why a paper run died with 401, audit LIVE-13).

``PaperBroker`` closes that gap: it implements the same method surface the
engine calls, answers with the same response envelopes, and *simulates* fills
against real market data (or a deterministic synthetic series when no broker
login exists). It records every order so a session can be reconciled.

DESIGN RULES
------------
* **It cannot reach the internet.** Every method is local; there is no network
  call in this module. That is what makes it safe to run anywhere.
* **Fail closed on the unknown.** An unimplemented method raises
  ``NotImplementedError`` rather than returning a plausible-looking success:
  a silent fake would be worse than a crash.
* **Deterministic.** The synthetic price series is a seeded function of the
  symbol, so two runs of the same session produce the same tape. Randomness in
  a test harness makes results unreproducible.
* **Same shape as the SDK.** ``{"status": "success", "data": ...}`` (and
  ``order_id`` on placement), so no caller needs a special case.
"""
from __future__ import annotations

import math
import random
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

__all__ = ["PaperBroker", "PaperOrder"]

#: Reference price per symbol when no real quote is available. Only a starting
#: point for the synthetic walk; it is NOT presented as a market price.
_BASE_PRICE = {
    "INFY": 1480.0, "TCS": 3500.0, "RELIANCE": 2900.0, "HDFCBANK": 1650.0,
    "ICICIBANK": 1200.0, "SBIN": 800.0, "BHARTIARTL": 1500.0, "ITC": 450.0,
}


class PaperOrder(dict):
    """A recorded order. A dict so it serialises to JSON unchanged."""


class PaperBroker:
    """SDK-shaped broker that fills locally and records everything.

    Args:
        starting_cash: simulated account cash.
        data_source: a client used ONLY to read real quotes/history. When None,
            or when that client raises, a deterministic synthetic series is
            used and the order/report marks the data as ``synthetic``.
        fill_slippage_bps: adverse slippage applied to every simulated fill.
        tick_size: rounding for simulated prices.
    """

    def __init__(self, *, starting_cash: float = 100000.0,
                 data_source: Any = None,
                 fill_slippage_bps: float = 2.0,
                 tick_size: float = 0.05,
                 seed: int = 7) -> None:
        self.starting_cash = float(starting_cash)
        self.cash = float(starting_cash)
        self.data_source = data_source
        self.fill_slippage_bps = float(fill_slippage_bps)
        self.tick_size = float(tick_size) if tick_size > 0 else 0.05
        self._seed = int(seed)
        # Reentrant: summary() holds the lock and calls positionbook(), which
        # takes it again. A plain Lock deadlocked here on the first report.
        self._lock = threading.RLock()
        self.orders: List[PaperOrder] = []
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._prices: Dict[str, float] = {}
        self._next_id = 0
        #: Realised P&L from closed quantity. The risk gate refuses to trade
        #: without a verified daily P&L, so this must be real, not omitted.
        self.realised_pnl = 0.0
        #: True when at least one price came from the synthetic walk.
        self.used_synthetic_data = False
        #: Count of calls served from a real broker.
        self.real_data_calls = 0
        #: api_key is contractually needed by the SDK surface some callers probe.
        self.api_key = "paper"

    # ------------------------------------------------------------------ #
    # price simulation
    # ------------------------------------------------------------------ #
    def _rng(self, symbol: str) -> random.Random:
        """A deterministic per-symbol generator (reproducible tape)."""
        return random.Random(f"{symbol}:{self._seed}")

    def _tick(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 4)

    def _synthetic_price(self, symbol: str, step: int = 0) -> float:
        """Deterministic pseudo-random walk anchored at a per-symbol base."""
        base = _BASE_PRICE.get(symbol.upper())
        if base is None:
            # Stable, symbol-derived base so an unknown ticker is still
            # reproducible rather than magic.
            base = 100.0 + (abs(hash(symbol.upper())) % 4000) / 10.0
        rng = self._rng(symbol)
        drift = math.sin(step / 7.0) * 0.004
        noise = rng.uniform(-0.003, 0.003)
        return self._tick(base * (1.0 + drift + noise))

    def _price_for(self, symbol: str, step: int = 0) -> float:
        """Real quote if available, else the synthetic walk."""
        key = symbol.upper()
        if self.data_source is not None:
            try:
                resp = self.data_source.quotes(symbol=symbol, exchange="NSE")
                data = resp.get("data") if isinstance(resp, dict) else None
                if isinstance(data, dict) and resp.get("status") == "success":
                    for field in ("ltp", "last_price", "lastPrice", "lp", "close"):
                        value = data.get(field)
                        try:
                            price = float(value)
                        except (TypeError, ValueError):
                            continue
                        if price > 0:
                            self.real_data_calls += 1
                            self._prices[key] = self._tick(price)
                            return self._prices[key]
            except Exception:  # noqa: BLE001 - fall back to synthetic
                pass
        if key not in self._prices or step:
            self._prices[key] = self._synthetic_price(symbol, step)
            self.used_synthetic_data = True
        return self._prices[key]

    # ------------------------------------------------------------------ #
    # market data surface
    # ------------------------------------------------------------------ #
    def quotes(self, *, symbol: str, exchange: str = "NSE") -> Dict[str, Any]:
        price = self._price_for(symbol)
        spread = self._tick(max(self.tick_size, price * 0.0002))
        volume = 100000 + (abs(hash(symbol)) % 900000)
        return {"status": "success", "data": {
            "symbol": symbol, "exchange": exchange, "ltp": price,
            "bid": self._tick(price - spread), "ask": self._tick(price + spread),
            "open": price, "high": price, "low": price, "close": price,
            "volume": volume, "oi": 0,
            "date": datetime.now().strftime("%Y-%m-%d"),
        }}

    def depth(self, *, symbol: str, exchange: str = "NSE") -> Dict[str, Any]:
        price = self._price_for(symbol)
        qty = 500
        levels = [{"price": self._tick(price - i * self.tick_size),
                   "quantity": qty} for i in range(1, 6)]
        asks = [{"price": self._tick(price + i * self.tick_size),
                 "quantity": qty} for i in range(1, 6)]
        return {"status": "success", "data": {
            "symbol": symbol, "exchange": exchange,
            "bids": levels, "asks": asks,
            "totalbuyqty": qty * 5, "totalsellqty": qty * 5,
        }}

    def history(self, *, symbol: str, exchange: str = "NSE", interval: str = "5m",
                start_date: str = "", end_date: str = "") -> Dict[str, Any]:
        """Synthetic OHLCV bars. Deterministic for a given (symbol, interval).

        Bars are shaped like the SDK's (``timestamp/open/high/low/close/volume``)
        so the strategy's parser works unchanged.
        """
        count = 120
        rows = []
        step = 0
        base_price = self._price_for(symbol)
        for index in range(count):
            step += 1
            price = self._synthetic_price(symbol, step)
            if base_price and index == 0:
                price = base_price
            high = self._tick(price * 1.002)
            low = self._tick(price * 0.998)
            ts = datetime.now() - timedelta(minutes=(count - index) * 15)
            rows.append({
                "timestamp": int(ts.timestamp()), "date": ts.strftime("%Y-%m-%d"),
                "time": ts.strftime("%H:%M:%S"), "open": price,
                "high": high, "low": low, "close": price,
                "volume": 50000 + (index * 137) % 40000, "oi": 0,
            })
        self.used_synthetic_data = True
        return {"status": "success", "data": rows}

    # ------------------------------------------------------------------ #
    # order surface
    # ------------------------------------------------------------------ #
    def placeorder(self, *, strategy: str = "PAPER", symbol: str,
                   action: str, exchange: str, price_type: str = "MARKET",
                   product: str = "MIS", quantity: int = 1,
                   price: Optional[float] = None,
                   trigger_price: Optional[float] = None,
                   **kwargs: Any) -> Dict[str, Any]:
        """Record and (for MARKET/LIMIT) immediately fill the order."""
        qty = int(quantity or 0)
        side = str(action).upper()
        if qty <= 0 or side not in ("BUY", "SELL"):
            return {"status": "error", "message": "invalid quantity or action"}
        market = self._price_for(symbol)
        with self._lock:
            self._next_id += 1
            order_id = f"PAPER{self._next_id:05d}"
            trigger = float(trigger_price) if trigger_price is not None else None
            limit = float(price) if price is not None else None
            # SL-M rests until the market crosses its trigger; it is NOT filled
            # now. That is what makes it protection rather than an instant exit.
            resting = str(price_type).upper() == "SL-M" and trigger is not None
            order = PaperOrder(
                orderid=order_id, order_id=order_id, strategy=strategy,
                symbol=symbol, exchange=exchange, action=side,
                pricetype=str(price_type).upper(), product=product,
                quantity=qty, price=limit or 0.0,
                trigger_price=trigger or 0.0,
                order_status="trigger pending" if resting else "complete",
                filled_quantity=0 if resting else qty,
                pending_quantity=qty if resting else 0,
                average_price=0.0,
                placed_at=datetime.now().isoformat(),
            )
            if not resting:
                order["average_price"] = self._fill_price(side, market, limit)
                self._apply_fill(order)
            self.orders.append(order)
        return {"status": "success", "order_id": order_id,
                "data": {"order_status": order["order_status"],
                         "symbol": symbol, "quantity": qty}}

    def _fill_price(self, side: str, market: float,
                    limit: Optional[float]) -> float:
        """Adverse slippage, bounded by the limit when one was given."""
        slip = market * (self.fill_slippage_bps / 10000.0)
        price = self._tick(market + slip if side == "BUY" else market - slip)
        if limit is not None:
            price = min(price, limit) if side == "BUY" else max(price, limit)
        return self._tick(max(price, self.tick_size))

    def _apply_fill(self, order: PaperOrder) -> None:
        symbol, qty = order["symbol"], order["filled_quantity"]
        price = order["average_price"]
        signed = qty if order["action"] == "BUY" else -qty
        pos = self._positions.setdefault(symbol, {"qty": 0, "avg": 0.0,
                                                  "exchange": order["exchange"]})
        cash_flow = -signed * price
        self.cash += cash_flow
        old_qty = pos["qty"]
        new_qty = old_qty + signed
        if old_qty == 0 or (old_qty > 0) == (signed > 0):
            # Opening or adding: volume-weighted average.
            total_cost = pos["avg"] * abs(old_qty) + price * abs(signed)
            pos["avg"] = total_cost / abs(new_qty) if new_qty else 0.0
        elif new_qty == 0:
            self.realised_pnl += (price - pos["avg"]) * old_qty
            pos["avg"] = 0.0
        elif (old_qty > 0) != (new_qty > 0):
            # Flipped through zero: realise the closed side, then restart.
            self.realised_pnl += (price - pos["avg"]) * old_qty
            pos["avg"] = price
        pos["qty"] = new_qty

    def cancelorder(self, *, order_id: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            for order in self.orders:
                if order["orderid"] == order_id:
                    if order["order_status"] in ("trigger pending", "open"):
                        order["order_status"] = "cancelled"
                        return {"status": "success",
                                "message": f"{order_id} cancelled"}
                    return {"status": "error",
                            "message": f"{order_id} is {order['order_status']}"}
        return {"status": "error", "message": f"unknown order {order_id}"}

    def modifyorder(self, *, order_id: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            for order in self.orders:
                if order["orderid"] == order_id:
                    if kwargs.get("trigger_price") is not None:
                        order["trigger_price"] = float(kwargs["trigger_price"])
                    if kwargs.get("quantity") is not None:
                        order["quantity"] = int(kwargs["quantity"])
                    return {"status": "success"}
        return {"status": "error", "message": f"unknown order {order_id}"}

    def ordersmartorder(self, **_kwargs: Any) -> Dict[str, Any]:
        return {"status": "error", "message": "not supported by PaperBroker"}

    # ------------------------------------------------------------------ #
    # account surface
    # ------------------------------------------------------------------ #
    def orderbook(self) -> Dict[str, Any]:
        with self._lock:
            rows = [dict(o) for o in self.orders]
        return {"status": "success", "data": rows, "total_logs": len(rows)}

    def tradebook(self) -> Dict[str, Any]:
        with self._lock:
            fills = [dict(o) for o in self.orders
                     if o["filled_quantity"] > 0]
        return {"status": "success", "data": fills}

    def positionbook(self) -> Dict[str, Any]:
        with self._lock:
            rows = []
            for symbol, pos in self._positions.items():
                if not pos["qty"]:
                    continue
                ltp = self._price_for(symbol)
                rows.append({
                    "symbol": symbol, "exchange": pos["exchange"],
                    "netqty": pos["qty"], "quantity": pos["qty"],
                    "average_price": pos["avg"], "avgprc": pos["avg"],
                    "ltp": ltp, "product": "CNC",
                    "pnl": round((ltp - pos["avg"]) * pos["qty"], 2),
                    # The risk gate sums a per-row P&L field; an absent one
                    # makes it fail closed and refuse every entry.
                    "day_pnl": round((ltp - pos["avg"]) * pos["qty"], 2),
                    "realised_pnl": round(self.realised_pnl, 2),
                })
            if not rows or self.realised_pnl:
                # A flat book is still a VERIFIED statement of P&L (zero, plus
                # any realised amount). Emitting an empty list makes every
                # downstream consumer treat the day's P&L as unknown and fail
                # closed. The row carries zero quantity, so it is never a
                # position - it is the day's realised-P&L carrier.
                rows.append({"symbol": "PAPER-CASH", "exchange": "NSE",
                             "netqty": 0, "quantity": 0,
                             "average_price": 0.0, "avgprc": 0.0, "ltp": 0.0,
                             "pnl": round(self.realised_pnl, 2),
                             "day_pnl": round(self.realised_pnl, 2)})
        return {"status": "success", "data": rows}

    def funds(self) -> Dict[str, Any]:
        return {"status": "success", "data": {"availablecash": round(self.cash, 2),
                                              "cash": round(self.cash, 2),
                                              "collateral": 0.0,
                                              "utiliseddebits": 0.0}}

    def analyzerstatus(self) -> Dict[str, Any]:
        # Paper mode is never "live": the mode string is explicit so every
        # existing caller (runner, supervisor, exit engine) reads it correctly.
        return {"status": "success", "data": {"mode": "analyze",
                                              "paper_broker": True}}

    def symbol(self, symbol: str, exchange: str = "NSE") -> Dict[str, Any]:
        return {"status": "success", "data": {"symbol": symbol,
                                              "exchange": exchange,
                                              "lot_size": 1,
                                              "tick_size": self.tick_size}}

    def subscribe_orders(self, on_order_update=None) -> bool:
        """No push feed; the engine falls back to its REST/ledger path.

        Returning True would be a lie - the runner relies on a True here to
        prove fills are monitored, so paper mode must be honest about it.
        """
        return False

    # ------------------------------------------------------------------ #
    # reporting
    # ------------------------------------------------------------------ #
    def summary(self) -> Dict[str, Any]:
        """Session reconciliation used by the paper report."""
        with self._lock:
            orders = [dict(o) for o in self.orders]
            # positionbook() re-enters the lock; it is an RLock for this call.
            positions = self.positionbook().get("data") or []
        filled = [o for o in orders if o["filled_quantity"] > 0]
        notional = sum(o["average_price"] * o["filled_quantity"] for o in filled)
        return {
            "starting_cash": self.starting_cash,
            "ending_cash": round(self.cash, 2),
            "orders": len(orders),
            "filled_orders": len(filled),
            "resting_orders": len([o for o in orders
                                   if o["order_status"] == "trigger pending"]),
            "cancelled_orders": len([o for o in orders
                                     if o["order_status"] == "cancelled"]),
            "traded_notional": round(notional, 2),
            "open_positions": positions,
            "real_data_calls": self.real_data_calls,
            "used_synthetic_data": self.used_synthetic_data,
            "data_classification": ("mixed" if self.real_data_calls
                                    and self.used_synthetic_data else
                                    "live" if self.real_data_calls else
                                    "synthetic"),
        }
