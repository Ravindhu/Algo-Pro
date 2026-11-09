"""
pov_peg.py (ported from Blue_engine/pov/slicers/peg.py, 2026-11-09; decouple 6.5 Phase C).
Status: PHASE 3 IMPLEMENTED (pure; state held per instance, no module state)

peg.py - PEG-1 passive/peg pricing engine for the POV slicer.

================================================================================
PURPOSE
================================================================================
At low capital the order size is tiny relative to top-of-book depth, so
market impact is NOT the problem - spread capture is. Institutional desks
do not sweep the book on small tickets; they JOIN the bid/ask and only
cross the spread when the market proves they must.

This module implements that policy as a pure, unit-testable state machine:

  1. PASSIVE      - place the LIMIT at the best bid (BUY) / best ask (SELL).
  2. CHASE        - re-peg upward only when the bid lifts, never beyond
                    ``peg_max_chase_ticks`` above the original anchor.
  3. CROSS        - escape to a marketable LIMIT (ask + buffer ticks for
                    BUY) when either:
                       a) the patience timer for this slice expires, or
                       b) the bid has lifted past the chase cap (market
                          is running away - do not be left behind).

The slicer feeds each poll's TopOfBook snapshot into ``PegEngine.decide``
and receives a price plus a reason tag. Nothing here touches the SDK,
the ledger or the network, so every rule is deterministic and testable.

CONFIG KEYS (read from the POV slicer config dict)
--------------------------------------------------
    peg_max_chase_ticks     int   (default 2)  max re-pegs away from anchor
    peg_patience_seconds    float (default 10) seconds before forced cross
    peg_escape_buffer_ticks int   (default 1)  ticks past the touch when crossing
    tick_size               float (optional)   used when depth omits it

================================================================================
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from quant.execution.pov_guards import TopOfBook


# Reason tags returned to the slicer (stable strings - dashboard/log greppable).
REASON_PASSIVE = "passive_at_touch"
REASON_REPEG = "repeg_within_cap"
REASON_CROSS_PATIENCE = "cross_patience_expired"
REASON_CROSS_RUNAWAY = "cross_market_ran_away"
REASON_HOLD = "hold_no_top"
REASON_RESET = "new_slice"

_DEFAULT_CHASE_TICKS = 2
_DEFAULT_PATIENCE_SECONDS = 10.0
_DEFAULT_ESCAPE_BUFFER_TICKS = 1


@dataclass
class PegState:
    """Per-slice pegging state.

    ``anchor_price`` is the touch price when this slice first went passive;
    every chase decision is measured from it, so a slow drift cannot walk
    the peg unbounded away from the original entry level.
    """

    anchor_price: float
    # The touch price when the slice FIRST went passive. The chase cap is
    # measured from this origin, not from the (moving) anchor, so a slow
    # drift cannot walk the peg unbounded away from the entry level.
    origin_price: float
    attempt_started_ts: float
    crossed: bool = False
    chase_count: int = 0


@dataclass
class PegDecision:
    """What the slicer should do with this poll's child order."""

    price: Optional[float] = None        # LIMIT price to place (None => skip)
    reason: str = REASON_HOLD            # one of the REASON_* tags
    state: Optional[PegState] = None     # updated state (persist across polls)
    metadata: Dict[str, Any] = field(default_factory=dict)


def _safe_int_cfg(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        raw = cfg.get(key)
        if raw is None or raw == "":
            return default
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return default


def _safe_float_cfg(cfg: Dict[str, Any], key: str, default: float) -> float:
    try:
        raw = cfg.get(key)
        if raw is None or raw == "":
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def _round_tick(price: float, tick: float) -> float:
    if tick > 0:
        return round(round(price / tick) * tick, 4)
    return round(price, 4)


class PegEngine:
    """Stateful per-execution pegging policy (one instance per run)."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.max_chase_ticks = _safe_int_cfg(
            cfg, "peg_max_chase_ticks", _DEFAULT_CHASE_TICKS)
        self.patience_seconds = _safe_float_cfg(
            cfg, "peg_patience_seconds", _DEFAULT_PATIENCE_SECONDS)
        self.escape_buffer_ticks = _safe_int_cfg(
            cfg, "peg_escape_buffer_ticks", _DEFAULT_ESCAPE_BUFFER_TICKS)
        self.state: Optional[PegState] = None

    # ------------------------------------------------------------------
    def reset_for_new_slice(self) -> None:
        """Call when a NEW slice quantity is about to be worked.

        Keeps the policy per-slice: each child gets its own fresh anchor
        and patience window, mirroring how a desk works one clip at a time.
        """
        self.state = None

    # ------------------------------------------------------------------
    def decide(
        self,
        top: TopOfBook,
        *,
        action: str,
        now_ts: float,
    ) -> PegDecision:
        """Compute the LIMIT price for this poll under the peg policy.

        Returns a PegDecision. ``price is None`` means "do not place a
        child this poll" (e.g. no usable top-of-book); the slicer treats
        that as skip-the-slice rather than falling back to MARKET, so the
        passive policy can never silently degrade into paying the spread.
        """
        action = str(action or "").upper()
        if action not in ("BUY", "SELL") or not top.has_top:
            return PegDecision(reason=REASON_HOLD)

        tick = top.tick_size or 0.05

        # First touch of this slice: anchor at the passive side.
        if self.state is None:
            anchor = top.best_bid if action == "BUY" else top.best_ask
            self.state = PegState(
                anchor_price=float(anchor),
                origin_price=float(anchor),
                attempt_started_ts=float(now_ts),
            )
            price = float(anchor)
            return PegDecision(
                price=_round_tick(price, tick),
                reason=REASON_PASSIVE,
                state=self.state,
                metadata={"anchor": anchor, "tick_size": tick},
            )

        st = self.state
        buffer_px = self.escape_buffer_ticks * tick

        if action == "BUY":
            chase_cap = st.origin_price + self.max_chase_ticks * tick
            if top.best_bid >= chase_cap + tick / 2:
                # Market ran away: cross at ask + buffer (marketable LIMIT).
                price = _round_tick(top.best_ask + buffer_px, tick)
                st.crossed = True
                return PegDecision(price=price, reason=REASON_CROSS_RUNAWAY,
                                   state=st)
            patience_over = (
                float(now_ts) - st.attempt_started_ts) >= self.patience_seconds
            if patience_over:
                price = _round_tick(top.best_ask + buffer_px, tick)
                st.crossed = True
                return PegDecision(price=price, reason=REASON_CROSS_PATIENCE,
                                   state=st)
            # Re-peg only upward, and only within the chase cap.
            if top.best_bid > st.anchor_price:
                st.chase_count += 1
                st.anchor_price = min(float(top.best_bid), chase_cap)
                return PegDecision(
                    price=_round_tick(st.anchor_price, tick),
                    reason=REASON_REPEG,
                    state=st,
                    metadata={"chase_count": st.chase_count},
                )
            # Price did not lift: hold the existing peg price.
            return PegDecision(
                price=_round_tick(st.anchor_price, tick),
                reason=REASON_PASSIVE,
                state=st,
            )

        # SELL mirror image.
        chase_cap = st.origin_price - self.max_chase_ticks * tick
        if top.best_ask <= chase_cap - tick / 2:
            price = _round_tick(top.best_bid - buffer_px, tick)
            st.crossed = True
            return PegDecision(price=price, reason=REASON_CROSS_RUNAWAY,
                               state=st)
        patience_over = (
            float(now_ts) - st.attempt_started_ts) >= self.patience_seconds
        if patience_over:
            price = _round_tick(top.best_bid - buffer_px, tick)
            st.crossed = True
            return PegDecision(price=price, reason=REASON_CROSS_PATIENCE,
                               state=st)
        if top.best_ask < st.anchor_price:
            st.chase_count += 1
            st.anchor_price = max(float(top.best_ask), chase_cap)
            return PegDecision(
                price=_round_tick(st.anchor_price, tick),
                reason=REASON_REPEG,
                state=st,
                metadata={"chase_count": st.chase_count},
            )
        return PegDecision(
            price=_round_tick(st.anchor_price, tick),
            reason=REASON_PASSIVE,
            state=st,
        )


def validate_peg_config(cfg: Dict[str, Any]) -> Optional[str]:
    """Return an error string for an invalid PEG config, else None."""
    try:
        chase = cfg.get("peg_max_chase_ticks")
        if chase is not None and int(float(chase)) < 0:
            return "peg_max_chase_ticks must be >= 0"
        patience = cfg.get("peg_patience_seconds")
        if patience is not None and float(patience) <= 0:
            return "peg_patience_seconds must be > 0"
        buf = cfg.get("peg_escape_buffer_ticks")
        if buf is not None and int(float(buf)) < 0:
            return "peg_escape_buffer_ticks must be >= 0"
    except (TypeError, ValueError):
        return "peg_* config values must be numeric"
    return None
