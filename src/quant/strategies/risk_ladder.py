"""Risk ladder for the cycle strategies (owner rules 2026-11-19, rules doc section 11 and 14).

Flow stage: Strategy bot cycle (sizing of each entry)
Status: PHASE 2 IMPLEMENTED (2026-11-19): pure logic, no broker, no orders sent. Plan: docs/IMPLEMENTATION_PLAN_2026-11-19_LADDER.md

Simple words:
- Every entry is sized at its own price against the HTF OCL low (HTF L):
      shares = floor(equity * risk / (entry - HTF L)) (+ carried shares)
- Each LTF OCL carries 0.25 % of fresh risk in total: 0.10 % at its aggressive touch,
  plus 0.15 % at its conservative touch of the SAME OCL (owner 2026-11-19: the 0.15 % is
  part of the 0.25 %, not on top).
- Stages (per HTF cycle):
  * probe: OCL #1 aggressive touch = 0.10 %; its conservative touch = +0.15 %.
  * ladder: a new OCL below the invalidated level: carried shares join its aggressive
    entry (carried + 0.10 %), then its conservative touch = +0.15 %.
    This runs for OCL #2 and OCL #3. After the OCL #3 entry, stop at the probe rate
    (owner 2026-11-19: carried shares then wait for the escalation pair).
  * waiting_pair: only the escalation pair can open a trade. A conservative touch arms
    the pair; then a NEW OCL (different id) whose aggressive level is touched = 0.50 %.
    One entry, no split, then back to waiting. Carried shares ARE added to the 0.50 % entry
    (owner 2026-11-19).
- Per-cycle loss ceiling: the planned risk at stop (qty x (price - HTF L)) of all orders in
  one HTF cycle may not exceed ceiling x equity. Orders that would go over are refused.

Numbers come from config/settings.yaml, section cycle (see load_cycle_settings).
Nothing here reads the clock or the broker. Pure functions and one small state class.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

PROBE_AGGRESSIVE = 0.0010   # first OCL, aggressive touch
PROBE_CONSERVATIVE = 0.0015  # same OCL, conservative touch (OCL total 0.25 %, with the 0.10 % above)
OCL_TOTAL = PROBE_AGGRESSIVE + PROBE_CONSERVATIVE  # 0.25 % fresh risk per LTF OCL (owner 2026-11-19)
CONVICTION = 0.0050         # escalation pair only, one-shot, re-earned
LADDER_OCL_LIMIT = 3        # after this many OCL entries, only the pair can open trades
_EPS = 1e-9


def shares_for(equity: float, risk: float, price: float, htf_low: float, carry: int = 0) -> int:
    """floor(equity * risk / (price - htf_low)) + carry. Zero when price is not above htf_low."""
    if equity <= 0 or risk <= 0 or price <= htf_low:
        return 0
    fresh = math.floor(equity * risk / (price - htf_low) + _EPS)
    return max(0, int(fresh) + int(carry))


def load_cycle_settings(path) -> Dict[str, float]:
    """target_r (N) and loss_ceiling from settings.yaml section 'cycle'. Missing file or key = None.

    None means NOT SET: the cycle refuses aggressive-only trades (fail closed).
    """
    import yaml as _yaml

    out = {"target_r": None, "loss_ceiling": None, "portfolio_loss_ceiling": None}
    if path is None or not Path(path).exists():
        return out
    data = _yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    sec = data.get("cycle") or {}
    for key in out:
        val = sec.get(key)
        if val is not None:
            if not isinstance(val, (int, float)) or float(val) <= 0:
                raise ValueError(f"cycle.{key} must be a positive number, got {val!r}")
            out[key] = float(val)
    for key in ("loss_ceiling", "portfolio_loss_ceiling"):
        if out[key] is not None and out[key] > 0.2:
            raise ValueError(f"cycle.{key} is a fraction of equity; 0.03 means 3 %")
    return out


@dataclass
class Order:
    """One planned entry. filled is updated by record_fill; unfilled rolls forward on invalidation."""
    ocl_id: str
    price: float
    qty: int
    risk: float
    reason: str  # probe_aggressive | probe_conservative | ladder | conviction
    filled: int = 0
    carry: int = 0  # carried shares included in qty (already counted in the parent's remaining)
    closed: bool = False  # its OCL was invalidated: the unfilled part now travels with the next order

    @property
    def unfilled(self) -> int:
        return 0 if self.closed else max(0, self.qty - self.filled)

    def to_doc(self) -> Dict:
        return {"ocl_id": self.ocl_id, "price": self.price, "qty": self.qty, "risk": self.risk,
                "reason": self.reason, "filled": self.filled, "carry": self.carry,
                "closed": self.closed}

    @staticmethod
    def from_doc(d: Dict) -> "Order":
        return Order(ocl_id=str(d["ocl_id"]), price=float(d["price"]), qty=int(d["qty"]),
                     risk=float(d["risk"]), reason=str(d["reason"]), filled=int(d["filled"]),
                     carry=int(d.get("carry", 0)), closed=bool(d.get("closed", False)))


@dataclass
class RiskLadder:
    """State for one HTF cycle. Create a new one for each HTF cycle."""
    equity: float
    htf_low: float
    loss_ceiling: Optional[float] = None  # fraction of equity; None = not set = refuse all entries
    stage: str = "probe"                  # probe | waiting_pair
    ocl_entries: int = 0
    carry: int = 0
    armed_ocl: Optional[str] = None       # OCL whose conservative level was touched (pair armed)
    orders: List[Order] = field(default_factory=list)
    _current: Optional[Order] = None
    _current_ocl: Optional[str] = None
    _current_cons: bool = False
    last_refused: Optional[str] = None  # why the last touch planned no order: loss_ceiling (audit trail)

    # ---- helpers -------------------------------------------------------------------------
    def planned_risk_inr(self) -> float:
        return sum(o.qty * (o.price - self.htf_low) for o in self.orders)

    def _ceiling_allows(self, qty: int, price: float) -> bool:
        if self.loss_ceiling is None:
            return False  # not set: fail closed
        extra = qty * (price - self.htf_low)
        return self.planned_risk_inr() + extra <= self.loss_ceiling * self.equity + _EPS

    def _add(self, ocl_id: str, price: float, qty: int, risk: float, reason: str) -> Optional[Order]:
        if qty < 1:
            return None
        if not self._ceiling_allows(qty, price):
            self.last_refused = "loss_ceiling"
            return None
        order = Order(ocl_id=ocl_id, price=price, qty=qty, risk=risk, reason=reason)
        self.orders.append(order)
        return order

    # ---- events --------------------------------------------------------------------------
    def aggressive_touch(self, ocl_id: str, price: float) -> Optional[Order]:
        """An OCL's aggressive level was touched. Returns the planned order, or None."""
        self.last_refused = None
        if price <= self.htf_low:
            return None
        if self.stage == "waiting_pair":
            if self.armed_ocl is None or ocl_id == self.armed_ocl:
                return None  # the pair needs a NEW OCL formed after the conservative touch
            qty = shares_for(self.equity, CONVICTION, price, self.htf_low, self.carry)
            order = self._add(ocl_id, price, qty, CONVICTION, "conviction")
            if order is not None:
                order.carry = self.carry
                self.carry = 0  # carried shares are now in the 0.50 % entry
                self.armed_ocl = None  # back to waiting: the pair must fire again
            return order
        if self._current_ocl == ocl_id:
            return None  # this OCL already has its aggressive entry
        if self.ocl_entries == 0:
            risk, carry, reason = PROBE_AGGRESSIVE, 0, "probe_aggressive"
        else:
            risk, carry, reason = PROBE_AGGRESSIVE, self.carry, "ladder"  # carried shares join the 0.10 % entry
        qty = shares_for(self.equity, risk, price, self.htf_low, carry)
        order = self._add(ocl_id, price, qty, risk, reason)
        if order is None:
            return None
        order.carry = carry
        self.carry = 0  # carried shares are now in this order
        self.ocl_entries += 1
        self._current, self._current_ocl, self._current_cons = order, ocl_id, False
        if reason == "ladder" and self.ocl_entries >= LADDER_OCL_LIMIT:
            self.stage = "waiting_pair"  # after the third OCL entry, probe rate stops
        return order

    def conservative_touch(self, ocl_id: str, price: float) -> Optional[Order]:
        """A conservative level was touched.
        On the OCL that already has an entry (any stage, including the third OCL) it adds +0.15 %,
        so that OCL totals 0.25 % (owner 2026-11-19). On any other OCL it only arms the escalation pair."""
        self.last_refused = None
        if price <= self.htf_low:
            return None
        if self._current_ocl == ocl_id and not self._current_cons:
            self._current_cons = True
            self.armed_ocl = ocl_id
            qty = shares_for(self.equity, PROBE_CONSERVATIVE, price, self.htf_low)
            return self._add(ocl_id, price, qty, PROBE_CONSERVATIVE, "probe_conservative")
        if self.stage == "waiting_pair":
            self.armed_ocl = ocl_id
        return None

    def record_fill(self, order: Order, qty: int) -> None:
        order.filled = min(order.qty, order.filled + max(0, int(qty)))

    def active_order(self) -> Optional[Order]:
        """Oldest order that still has unfilled shares (fills go to the oldest first)."""
        for o in self.orders:
            if o.unfilled > 0:
                return o
        return None

    def to_doc(self) -> Dict:
        return {"equity": self.equity, "htf_low": self.htf_low, "loss_ceiling": self.loss_ceiling,
                "stage": self.stage, "ocl_entries": self.ocl_entries, "carry": self.carry,
                "armed_ocl": self.armed_ocl, "orders": [o.to_doc() for o in self.orders],
                "current_ocl": self._current_ocl, "current_cons": self._current_cons}

    @staticmethod
    def from_doc(d: Dict) -> "RiskLadder":
        r = RiskLadder(equity=float(d["equity"]), htf_low=float(d["htf_low"]),
                       loss_ceiling=d.get("loss_ceiling"), stage=str(d["stage"]),
                       ocl_entries=int(d["ocl_entries"]), carry=int(d["carry"]),
                       armed_ocl=d.get("armed_ocl"))
        r.orders = [Order.from_doc(x) for x in d.get("orders", [])]
        r._current_ocl = d.get("current_ocl")
        r._current_cons = bool(d.get("current_cons", False))
        r._current = next((o for o in r.orders if o.ocl_id == r._current_ocl), None)
        return r

    def invalidate(self, ocl_id: str) -> int:
        """A candle closed below this OCL's low. Its unfilled shares are carried forward.
        Returns the carried quantity (added to the next ladder entry)."""
        carried = 0
        for o in self.orders:
            if o.ocl_id == ocl_id and o.unfilled > 0:
                carried += o.unfilled
                o.closed = True  # its unfilled shares now travel with the next order (planned risk stays counted)
        self.carry += carried
        if self._current_ocl == ocl_id:
            self._current, self._current_ocl = None, None
        return carried
