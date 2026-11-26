"""PegSlicer: the 15 s peg model (2026-11-21), separate from the 5 min POV sizing in strategy_bot.

Status: PHASE 3 IMPLEMENTED (15 s peg slicer; orders go only through the injected executor)

The 5 min cycle sizes each parent (POV) and arms it with that cycle's allotment (PegAllotment).
Between cycles the slicer places verified pegs, spending only that allotment. It owns the peg price
search, the verifier call, peg submission with bounded reprice, and the report that goes to the next cycle.
It does not size parents, run risk checks or change parent state beyond the allotment and booking.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from quant.execution.peg import peg_candidates


@dataclass
class PegAllotment:
    """One parent's peg state for the current 5 min cycle."""
    waiting: bool = False          # may place pegs between cycles
    cap: Optional[float] = None    # entry price cap (from the cycle's bars)
    budget: int = 0                # shares allotted to this cycle (POV sizing, before peg caps)
    spent: int = 0                 # shares filled from the budget this cycle


def _empty_report() -> Dict[str, int]:
    return {"placed": 0, "filled_shares": 0, "unfilled": 0}


class PegSlicer:
    def __init__(self, *, cfg, executor, verifier, depth_provider, make_child: Callable,
                 log: Callable[..., None], alert: Callable[..., None], close: Callable[[str, str], None],
                 book: Callable[..., None], add_fault: Callable[[Any], None]) -> None:
        self.cfg = cfg
        self._executor = executor
        self._verifier = verifier
        self._depth = depth_provider
        self._make_child = make_child
        self._log = log
        self._alert = alert
        self._close = close
        self._book = book
        self._add_fault = add_fault
        self.status = ""              # "nodepth" | "unverified" | "ok" from the last price_now
        self.now_ts: Optional[float] = None
        self._report = _empty_report()

    # ---- cycle hooks --------------------------------------------------------------------------
    def start_cycle(self, ts: float) -> None:
        self.now_ts = ts

    def arm(self, p, cap: Optional[float], budget: int) -> None:
        """Set this cycle's allotment on a parent. A new cycle replaces it; nothing carries over."""
        p.peg = PegAllotment(waiting=(not p.cycle and budget >= 1), cap=cap,
                             budget=max(0, int(budget)), spent=0)

    def drain_report(self) -> Dict[str, int]:
        """15 s peg activity since the last cycle, for the next cycle's report. Resets the counters."""
        out = dict(self._report)
        self._report = _empty_report()
        return out

    # ---- price ----------------------------------------------------------------------------------
    def price_now(self, symbol: str, cap: Optional[float]) -> Optional[Tuple[float, int]]:
        """(order price, bid qty at the level), the first high-volume level that verifies, never above cap.

        Sets self.status: "nodepth" when there is no usable depth (caller uses the fixed path),
        "unverified" when no candidate passes the verifier (caller waits), "ok" otherwise.
        """
        self.status = "nodepth"
        if self._depth is None:
            return None
        try:
            depth = self._depth(symbol)
        except Exception as exc:  # noqa: BLE001 - a quote problem must not stop the bot
            self._log("peg_depth_error", symbol=symbol, error=repr(exc))
            return None
        cands = peg_candidates("BUY", depth, cap, self.cfg.peg_tick_size,
                               depth_levels=self.cfg.peg_depth_levels,
                               offset_ticks=self.cfg.peg_offset_ticks)
        if not cands:
            return None
        if self._verifier is None:
            self.status = "unverified"
            self._log("peg_no_verifier", symbol=symbol)
            return None
        now_ts = self.now_ts if self.now_ts is not None else time.time()
        for px, qty, lvl in cands[: self.cfg.peg_max_levels]:
            v = self._verifier.verdict(symbol, lvl, now_ts)
            if v.ok:
                self.status = "ok"
                self._log("peg_level_verified", symbol=symbol, level=lvl, price=px, qty=qty, metrics=v.metrics)
                return px, qty
            self._log("peg_level_unverified", symbol=symbol, level=lvl, reasons=list(v.reasons), metrics=v.metrics)
        self.status = "unverified"
        return None

    # ---- submit -----------------------------------------------------------------------------------
    def submit(self, p, qty: int, px: float, cap: Optional[float]):
        """One child as a LIMIT at the best bid. If it is not filled and was cancelled cleanly, reprice
        from the current bid, up to peg_max_attempts. A partial fill or a failed cancel stops at once."""
        outcome = None
        child = None
        qty_cap = qty
        sent_qty = qty  # what the LAST submitted child carried; a reprice may send less
        for attempt in range(self.cfg.peg_max_attempts):
            if attempt > 0:
                level = self.price_now(p.intent.symbol, cap)
                if level is None:
                    break
                px, bid_qty = level
                qty = min(qty_cap, int(bid_qty * self.cfg.peg_bid_fraction))
                if qty < 1:
                    break
                p.seq += 1
                self._log("peg_reprice", key=p.key, attempt=attempt + 1, px=px)
            child = self._make_child(p.intent, qty, p.seq)
            sent_qty = qty  # audit fix 3 (2026-11-26): book what was actually sent, not the first size
            outcome = self._executor.submit_child(child, "LIMIT", px)
            if not outcome.sent:
                break
            resp = outcome.response or {}
            if str(resp.get("status", "")).lower() == "success":
                break
            if resp.get("partial") or resp.get("cancel_failed"):
                break
            if "limit not filled" not in str(resp.get("reason", "")):
                break
        return outcome, child, sent_qty

    # ---- between-cycle tick -------------------------------------------------------------------------
    def tick(self, parents: Dict[str, Any], now: datetime) -> None:
        """Place pegs for waiting parents, within each parent's allotment. Gating (entries, faults, mode)
        is decided by the caller."""
        self.now_ts = now.timestamp()  # verifier freshness is measured from this tick
        for key, p in list(parents.items()):
            a = getattr(p, "peg", None)
            if a is None or not a.waiting or p.paused or p.remaining <= 0:
                continue
            budget_left = min(a.budget - a.spent, p.remaining)
            if budget_left < 1:
                continue
            level = self.price_now(p.intent.symbol, a.cap)
            if level is None:
                continue  # still unverified: try again on the next tick
            px, bid_qty = level
            qty = min(budget_left, int(bid_qty * self.cfg.peg_bid_fraction))
            if qty < 1:
                continue
            p.seq += 1
            outcome, _child, sent_qty = self.submit(p, qty, px, a.cap)
            self.settle(p, key, qty, outcome, sent_qty=sent_qty)

    # ---- booking of a peg child -----------------------------------------------------------------------
    def settle(self, p, key: str, qty: int, outcome, sent_qty: Optional[int] = None) -> None:
        """Book one peg child. Same rules as the cycle path: fills booked, unfilled cancelled, faults raised.
        ``sent_qty`` is what the (possibly repriced) child actually carried; a success books that,
        never the larger first-attempt size (audit fix 3, 2026-11-26)."""
        booked_qty = qty if sent_qty is None else int(sent_qty)  # fallback keeps the old signature
        if outcome is None or not outcome.sent:
            self._log("peg_not_sent", key=key)
            return
        self._report["placed"] += 1
        resp = outcome.response or {}
        status = str(resp.get("status", "")).lower()
        ref_px = float(p.intent.ref_price)
        if status == "success":
            self._book(p, key, None, booked_qty, ref_px, resp, {"children_sent": 0})
            self._report["filled_shares"] += booked_qty
            return
        if "limit not filled" in str(resp.get("reason", "")) and not resp.get("partial") \
                and not resp.get("cancel_failed"):
            self._report["unfilled"] += 1
            self._log("peg_unfilled", key=key, qty=qty)
            return
        filled = int(resp.get("filled_qty") or 0) if resp.get("partial") else 0
        if resp.get("partial") and filled > 0 and resp.get("avg_price"):
            filled = min(filled, booked_qty)
            self._book(p, key, None, filled, ref_px, {"avg_price": resp.get("avg_price")}, {"children_sent": 0})
            self._report["filled_shares"] += filled
            if resp.get("cancel_failed"):
                self._add_fault(("partial", key))
                self._alert("CRITICAL", f"{p.intent.symbol}: {filled} booked; the unfilled rest could not be "
                                        "cancelled. Entries blocked until checked with the broker")
            return
        if resp.get("partial"):
            self._add_fault(("partial", key))
            self._alert("CRITICAL", f"{p.intent.symbol}: partial peg fill with no fill quantity; "
                                    "entries blocked until reconciled with the broker")
        if resp.get("cancel_failed"):
            self._alert("CRITICAL", f"{p.intent.symbol}: unfilled peg could not be cancelled; check the broker")
        self._close(key, f"peg reply not success: {status or 'missing'}; parent halted")
