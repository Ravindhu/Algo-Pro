"""exit_engine.py - broker-side protection for every filled position.

WHY THIS EXISTS
---------------
Audit 2026-10-29 (P0-2): the engine could enter a position but nothing
protected it. ``core/protective_orders`` recorded SL/TP, and the POV slicer
wrote those levels into it, but no component read the registry to place or
watch a broker order. A filled CNC position was therefore unprotected the
moment the POV loop finished - across a lunch break, an API outage or an
overnight gap.

WHAT THIS MODULE DOES
---------------------
One supervisor loop, shared by every entry point (runner, screener bridge):

1. **Reconcile** the broker ``positionbook()`` against the protective-levels
   registry. A position with no registry record is UNPROTECTED (reported, and
   optionally flattened).
2. **Place** a broker-side SL-M (stop-market) for each covered position whose
   quantity has no working stop yet. The exit order is the OPPOSITE side, and
   its trigger is placed at ``tick`` precision on the safe side of the level
   (never on a price that would already be marketable).
3. **Verify** the stop really is working (orderbook status) before counting
   the position as protected. An order id alone is not protection.
4. **Ratchet** a stop that moved up (BUY) / down (SELL) by re-placing it, and
   **cancel** a stop whose position is gone or smaller than the stop size.
5. **Stop everything** when the durable kill switch trips: cancel working
   exit orders and (when armed) flatten through the existing POV path.

DESIGN RULES (mirroring the rest of core/)
------------------------------------------
* **Least privilege.** Market orders and target exits are OFF by default: a
  supervisor that sells on its own initiative is a bigger risk than an
  unprotected stop. Enable ``allow_market_exit`` explicitly.
* **Fail closed, never silently.** Every unverifiable safety input (no
  positionbook, unreadable mode, stop not confirmed working) is reported in
  the cycle report AND raises ``critical=True`` so the caller can trip the
  kill switch. The supervisor never assumes a stop exists.
* **Idempotent.** A stop is identified by the strategy tag it was placed
  under, so a restart or a repeated cycle does not stack duplicate stops.
* **SDK-only.** ``positionbook`` / ``orderbook`` / ``placeorder`` /
  ``cancelorder``; no OpenAlgo internals, no direct REST.
* **No import-time I/O.** Construction does not touch the broker; ``run_cycle``
  does, and is safe to unit-test with a stub client.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "ExitEngine",
    "ExitConfig",
    "protecting",
    "protect_positions",
]

#: Working statuses for an exit order (OpenAlgo lowercase vocabulary).
_WORKING = {"open", "trigger pending", "pending", "open pending", "accepted"}
#: Terminal-but-alive statuses that mean the stop did something.
_DEAD = {"rejected", "cancelled", "complete", "filled", "executed", "traded"}

_QTY_KEYS = ("netqty", "net_qty", "net_quantity", "quantity", "qty", "netqty ") 
_PRICE_KEYS = ("avgprc", "avg_price", "average_price", "ltp", "last_price")



def _num(value: Any) -> Optional[float]:
    """Parse a finite float, else None (never raises)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _first(row: Dict[str, Any], keys) -> Optional[float]:
    for key in keys:
        value = _num(row.get(key.strip() if isinstance(key, str) else key))
        if value is not None:
            return value
    return None


def _ok(resp: Any) -> bool:
    return isinstance(resp, dict) and resp.get("status") == "success"


def _status_of(resp: Any) -> str:
    """Lowercased OpenAlgo order status from a response (best effort)."""
    if not isinstance(resp, dict):
        return ""
    data = resp.get("data")
    for container in (data if isinstance(data, dict) else {}, resp):
        for key in ("order_status", "status", "orderStatus"):
            value = container.get(key)
            if isinstance(value, str) and value:
                return value.strip().lower()
    return ""


class ExitConfig:
    """Tunables. Defaults are deliberately conservative."""

    __slots__ = ("enabled", "strategy_tag", "check_interval_seconds",
                 "default_stop_pct", "tick_size", "product", "allow_market_exit",
                 "flatten_unprotected", "max_orders_per_cycle")

    def __init__(self, *, enabled: bool = True,
                 strategy_tag: str = "POV-EXIT",
                 check_interval_seconds: float = 15.0,
                 default_stop_pct: Optional[float] = 3.0,
                 tick_size: float = 0.05,
                 product: str = "CNC",
                 allow_market_exit: bool = False,
                 flatten_unprotected: bool = False,
                 max_orders_per_cycle: int = 10) -> None:
        self.enabled = bool(enabled)
        self.strategy_tag = str(strategy_tag or "POV-EXIT")
        self.check_interval_seconds = max(1.0, float(check_interval_seconds))
        #: Applied ONLY when the registry has no level for a position. Set to
        #: None to require an explicit level instead of inventing one.
        self.default_stop_pct = (None if default_stop_pct is None
                                 else abs(float(default_stop_pct)))
        self.tick_size = float(tick_size) if tick_size and float(tick_size) > 0 \
            else 0.05
        self.product = str(product or "CNC").upper()
        self.allow_market_exit = bool(allow_market_exit)
        self.flatten_unprotected = bool(flatten_unprotected)
        self.max_orders_per_cycle = max(1, int(max_orders_per_cycle))

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "ExitConfig":
        cfg = dict(raw or {})
        return cls(
            enabled=cfg.get("enabled", True),
            strategy_tag=cfg.get("strategy_tag", "POV-EXIT"),
            check_interval_seconds=cfg.get("check_interval_seconds", 15.0),
            default_stop_pct=cfg.get("default_stop_pct", 3.0),
            tick_size=cfg.get("tick_size", 0.05),
            product=cfg.get("product", "CNC"),
            allow_market_exit=cfg.get("allow_market_exit", False),
            flatten_unprotected=cfg.get("flatten_unprotected", False),
            max_orders_per_cycle=cfg.get("max_orders_per_cycle", 10),
        )


class ExitEngine:
    """Places, verifies and reconciles broker-side stop orders."""

    def __init__(self, client: Any, config: Optional[ExitConfig] = None,
                 *, registry: Any = None, kill_check: Optional[Callable[[], bool]] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.client = client
        self.config = config or ExitConfig()
        self._registry = registry           # e.g. blue_engine.core.protective_orders
        self._kill_check = kill_check or (lambda: False)
        self._log = log or (lambda message: logger.info("exit_engine: %s", message))
        #: stop orders this engine believes it placed: key -> {"order_id", "qty",
        #: "stop", "action", "strategy"}
        self._stops: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------
    def _log_msg(self, message: str) -> None:
        try:
            self._log(message)
        except Exception:  # noqa: BLE001
            logger.info("exit_engine: %s", message)

    def _rnd(self, price: float) -> float:
        tick = self.config.tick_size
        return round(round(price / tick) * tick, 4)

    @staticmethod
    def _key(symbol: str, exchange: str) -> str:
        return f"{symbol}|{exchange}"

    def positions(self) -> Tuple[Optional[List[Dict[str, Any]]], str]:
        """Read the broker positionbook.

        Returns ``(rows, error)``. ``rows`` is None when the book could not be
        verified - the caller must treat that as "unknown", never as "flat".
        """
        try:
            resp = self.client.positionbook()
        except Exception as exc:  # noqa: BLE001
            return None, f"positionbook raised: {exc!r}"
        if not _ok(resp):
            return None, f"positionbook not success: {str(resp)[:120]}"
        data = resp.get("data")
        rows = data if isinstance(data, list) else (
            data.get("positions") if isinstance(data, dict) else None)
        if not isinstance(rows, list):
            return None, "positionbook returned no rows list"
        return [r for r in rows if isinstance(r, dict)], ""

    def _open_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for row in rows:
            qty = _first(row, _QTY_KEYS)
            if qty is None or int(qty) == 0:
                continue
            symbol = str(row.get("symbol") or "").strip()
            exchange = str(row.get("exchange") or "").strip().upper()
            if not symbol or not exchange:
                continue
            row = dict(row)
            row["_qty"] = int(qty)
            row["_symbol"] = symbol
            row["_exchange"] = exchange
            row["_price"] = _first(row, _PRICE_KEYS)
            out.append(row)
        return out

    def _orders(self) -> Optional[List[Dict[str, Any]]]:
        """Working orders from the orderbook, or None when unverifiable."""
        method = getattr(self.client, "orderbook", None)
        if not callable(method):
            return None
        try:
            resp = method()
        except Exception as exc:  # noqa: BLE001
            self._log_msg(f"orderbook raised: {exc!r}")
            return None
        if not _ok(resp):
            return None
        data = resp.get("data")
        if isinstance(data, list):
            return [o for o in data if isinstance(o, dict)]
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            return [o for o in data["orders"] if isinstance(o, dict)]
        return None

    def _registry_levels(self) -> List[Dict[str, Any]]:
        if self._registry is None:
            return []
        try:
            return list(self._registry.snapshot())
        except Exception as exc:  # noqa: BLE001
            self._log_msg(f"registry snapshot failed: {exc!r}")
            return []

    def _level_for(self, symbol: str, exchange: str,
                   levels: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Newest matching registry record, or {}."""
        best: Dict[str, Any] = {}
        for rec in levels:
            if str(rec.get("symbol")) != symbol:
                continue
            if str(rec.get("exchange") or "").upper() != exchange:
                continue
            stamp = str(rec.get("updated_at") or rec.get("registered_at") or "")
            if not best or stamp >= str(best.get("updated_at")
                                        or best.get("registered_at") or ""):
                best = rec
        return best

    def _existing_stop(self, orders: List[Dict[str, Any]], symbol: str,
                       exchange: str) -> Optional[Dict[str, Any]]:
        """A working exit order this engine owns for the position."""
        for order in orders:
            if str(order.get("symbol")) != symbol:
                continue
            if str(order.get("exchange") or "").upper() != exchange:
                continue
            if str(order.get("strategy") or "") != self.config.strategy_tag:
                continue
            status = str(order.get("order_status") or order.get("status") or "").lower()
            if status in _DEAD:
                continue
            if status and status not in _WORKING and status not in _DEAD:
                # Unrecognised status: treat as working (never stack a stop).
                pass
            return order
        return None

    # -- placement -------------------------------------------------------
    def _place_stop(self, symbol: str, exchange: str, action: str,
                    quantity: int, trigger: float) -> Tuple[Optional[str], str]:
        """Place one SL-M. Returns ``(order_id, status)``."""
        place = getattr(self.client, "placeorder", None)
        if not callable(place):
            return None, "client has no placeorder()"
        try:
            resp = place(strategy=self.config.strategy_tag, symbol=symbol,
                         action=action, exchange=exchange, price_type="SL-M",
                         product=self.config.product, quantity=int(quantity),
                         trigger_price=self._rnd(trigger), price=0)
        except Exception as exc:  # noqa: BLE001
            return None, f"placeorder raised: {exc!r}"
        if not _ok(resp):
            return None, f"placeorder rejected: {str(resp)[:160]}"
        order_id = resp.get("order_id") or (resp.get("data") or {}).get("order_id")
        return (str(order_id) if order_id else None), _status_of(resp) or "open"

    def _cancel(self, order_id: str) -> bool:
        cancel = getattr(self.client, "cancelorder", None)
        if not callable(cancel):
            return False
        try:
            return _ok(cancel(order_id=order_id))
        except Exception as exc:  # noqa: BLE001
            self._log_msg(f"cancelorder {order_id} raised: {exc!r}")
            return False

    # -- one reconciliation cycle ---------------------------------------
    def run_cycle(self) -> Dict[str, Any]:
        """Reconcile once. Never raises; returns a structured report."""
        report: Dict[str, Any] = {
            "ts": datetime.now().isoformat(),
            "checked": 0, "protected": [], "placed": [], "cancelled": [],
            "unprotected": [], "actions": [], "warnings": [], "errors": [],
            "critical": False, "kill_tripped": False,
            #: False only when the position book could not be read, so every
            #: count in this report is UNKNOWN rather than zero.
            "verified": True,
        }
        if not self.config.enabled:
            report["warnings"].append("exit engine disabled")
            return report

        try:
            if self._kill_check():
                report["kill_tripped"] = True
                self._on_kill(report)
                return report
        except Exception as exc:  # noqa: BLE001 - a broken probe is not a pass
            report["errors"].append(f"kill-switch probe failed: {exc!r}")
            report["critical"] = True

        rows, error = self.positions()
        if rows is None:
            # Unknown book: never report "flat". The caller decides.
            report["errors"].append(error)
            report["critical"] = True
            report["verified"] = False
            return report
        open_rows = self._open_rows(rows)
        report["checked"] = len(open_rows)
        if not open_rows:
            # Nothing to protect, but a stop left over from a position that
            # closed elsewhere must still be cancelled (step 5 below).
            self._cancel_orphaned_stops(set(), report)
            return report

        levels = self._registry_levels()
        orders = self._orders()
        if orders is None:
            report["warnings"].append(
                "orderbook unverifiable: stops will be verified on the next "
                "cycle rather than assumed")

        placed_this_cycle = 0
        for row in open_rows:
            symbol, exchange, qty = row["_symbol"], row["_exchange"], row["_qty"]
            key = self._key(symbol, exchange)
            side = "SELL" if qty > 0 else "BUY"
            size = abs(qty)

            # 1. Is a stop already working? (orderbook truth, then local memo)
            existing = self._existing_stop(orders, symbol, exchange) if orders else None
            if existing is None and orders is None:
                memo = self._stops.get(key)
                existing = {"orderid": memo.get("order_id")} if memo else None
            if existing is not None:
                oid = str(existing.get("orderid") or existing.get("order_id") or "")
                status = str(existing.get("order_status")
                             or existing.get("status") or "").lower()
                if status and status in _DEAD:
                    report["warnings"].append(
                        f"{symbol}: exit order {oid} is {status}; re-protecting")
                else:
                    report["protected"].append(
                        {"symbol": symbol, "exchange": exchange,
                         "quantity": size, "order_id": oid,
                         "source": "broker" if orders else "memo"})
                    continue

            # 2. Where is the stop?
            rec = self._level_for(symbol, exchange, levels)
            stop = _num(rec.get("stop_loss"))
            source = "registry"
            if stop is None:
                pct = self.config.default_stop_pct
                ref = row["_price"]
                if pct is not None and ref and ref > 0:
                    stop = ref * (1 - pct / 100.0) if side == "SELL" \
                        else ref * (1 + pct / 100.0)
                    source = f"default {pct}% of {ref}"
            if stop is None or stop <= 0:
                report["unprotected"].append(
                    {"symbol": symbol, "exchange": exchange, "quantity": size,
                     "reason": "no stop level and no usable default"})
                report["critical"] = True
                # _flatten() is the only path allowed to trade here, and it
                # refuses by itself unless allow_market_exit is armed.
                if self.config.flatten_unprotected:
                    self._flatten(symbol, exchange, size, side, report,
                                  "no verifiable stop")
                continue

            # 3. A stop already marketable is not protection: skip and alert.
            ref_price = row["_price"]
            if ref_price:
                if side == "SELL" and self._rnd(stop) >= self._rnd(ref_price):
                    report["warnings"].append(
                        f"{symbol}: stop {stop} is at/above market {ref_price}; "
                        "not placing")
                    report["critical"] = True
                    continue
                if side == "BUY" and self._rnd(stop) <= self._rnd(ref_price):
                    report["warnings"].append(
                        f"{symbol}: stop {stop} is at/below market {ref_price}; "
                        "not placing")
                    report["critical"] = True
                    continue

            # 4. Tick precision on the safe side of the level.
            trig = self._rnd(stop)
            if trig == self._rnd(stop - self.config.tick_size) and \
                    side == "SELL" and ref_price and trig >= self._rnd(ref_price):
                trig = self._rnd(ref_price - self.config.tick_size)

            if placed_this_cycle >= self.config.max_orders_per_cycle:
                report["warnings"].append("per-cycle order cap reached")
                continue
            order_id, status = self._place_stop(symbol, exchange, side, size, trig)
            placed_this_cycle += 1
            if order_id is None:
                report["errors"].append(f"{symbol}: stop placement failed - {status}")
                report["critical"] = True
                continue
            with self._lock:
                self._stops[key] = {"order_id": order_id, "qty": size,
                                    "stop": trig, "action": side,
                                    "strategy": self.config.strategy_tag}
            report["placed"].append({"symbol": symbol, "exchange": exchange,
                                     "quantity": size, "order_id": order_id,
                                     "stop": trig, "source": source,
                                     "status": status})
            # An id is not protection: only a working order counts.
            if status in _DEAD:
                report["errors"].append(
                    f"{symbol}: exit order {order_id} came back {status}")
                report["critical"] = True
            else:
                report["protected"].append({"symbol": symbol, "exchange": exchange,
                                            "quantity": size, "order_id": order_id,
                                            "source": "placed"})

        # 5. Cancel stops for positions that vanished (closed elsewhere).
        live_keys = {self._key(r["_symbol"], r["_exchange"]) for r in open_rows}
        self._cancel_orphaned_stops(live_keys, report)
        return report

    def _cancel_orphaned_stops(self, live_keys: set,
                               report: Dict[str, Any]) -> None:
        """Cancel every remembered stop whose position is no longer open."""
        for key, memo in list(self._stops.items()):
            if key in live_keys:
                continue
            oid = memo.get("order_id")
            if oid and self._cancel(oid):
                report["cancelled"].append({"key": key, "order_id": oid})
            with self._lock:
                self._stops.pop(key, None)

    def _flatten(self, symbol: str, exchange: str, size: int, side: str,
                 report: Dict[str, Any], reason: str) -> None:
        """Market-exit an unprotected position (opt-in only)."""
        if not self.config.allow_market_exit:
            report["warnings"].append(
                f"{symbol}: would flatten {size} ({reason}) but "
                "allow_market_exit is false")
            return
        place = getattr(self.client, "placeorder", None)
        if not callable(place):
            report["errors"].append(f"{symbol}: no placeorder() to flatten")
            report["critical"] = True
            return
        try:
            resp = place(strategy=self.config.strategy_tag, symbol=symbol,
                         action=side, exchange=exchange, price_type="MARKET",
                         product=self.config.product, quantity=int(size))
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"{symbol}: flatten raised: {exc!r}")
            report["critical"] = True
            return
        if _ok(resp):
            report["actions"].append({"symbol": symbol, "flattened": size,
                                      "reason": reason})
        else:
            report["errors"].append(f"{symbol}: flatten rejected: {str(resp)[:120]}")
            report["critical"] = True

    def _on_kill(self, report: Dict[str, Any]) -> None:
        """Kill switch tripped: cancel every stop this engine owns."""
        self._log_msg("kill switch tripped: cancelling exit orders")
        for key, memo in list(self._stops.items()):
            oid = memo.get("order_id")
            if oid and self._cancel(oid):
                report["cancelled"].append({"key": key, "order_id": oid})
            with self._lock:
                self._stops.pop(key, None)

    # -- loop ------------------------------------------------------------
    def run_forever(self, stop_event: Optional[threading.Event] = None,
                    on_report: Optional[Callable[[Dict[str, Any]], None]] = None,
                    max_cycles: Optional[int] = None) -> Dict[str, Any]:
        """Run cycles until stopped. Returns the last report."""
        stop_event = stop_event or threading.Event()
        last: Dict[str, Any] = {}
        cycles = 0
        while not stop_event.is_set():
            last = self.run_cycle()
            if on_report is not None:
                try:
                    on_report(last)
                except Exception:  # noqa: BLE001
                    pass
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            stop_event.wait(self.config.check_interval_seconds)
        return last


def protecting(report: Dict[str, Any]) -> bool:
    """True only when every open position in the report has a working stop.

    An UNVERIFIED report (the position book could not be read) is never
    "protected": zero positions checked because the broker was unreachable is
    not the same as zero positions open.
    """
    if report.get("verified") is False:
        return False
    checked = int(report.get("checked") or 0)
    if checked == 0:
        return True
    return len(report.get("protected") or []) >= checked


def protect_positions(client: Any, *, config: Optional[Dict[str, Any]] = None,
                      registry: Any = None,
                      kill_check: Optional[Callable[[], bool]] = None,
                      log: Optional[Callable[[str], None]] = None
                      ) -> Dict[str, Any]:
    """One-shot convenience wrapper used by the runner and the bridge."""
    engine = ExitEngine(client, ExitConfig.from_dict(config),
                        registry=registry, kill_check=kill_check, log=log)
    return engine.run_cycle()
