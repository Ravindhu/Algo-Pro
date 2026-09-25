"""
pov_state_machine.py - per-symbol POV (Percentage-of-Volume) state machine.

Each watched symbol has its own :class:`PovStateMachine` that
transitions between states as the LTP walks through the OCL trigger
level and its surrounding bands.

States
------
WATCHING       - No active match. LTP observed but no action.
MATCHED        - HTF pattern (RRG / GRR) detected. We know the
                 trigger level but the LTP has not touched it yet.
ACCUMULATING   - LTP is in the touch zone of the trigger level.
                 The state machine emits "buy" intents at the POV
                 rate (qty_per_fire per tick for `investing`, or
                 risk-managed for `short_term`).
SLOW           - LTP dropped to the floor band. POV rate clamped to
                 pov_min_pct to avoid panic buying a falling knife.
STOPPED        - LTP reached the ceiling / accumulation target. No
                 more buys.
EXITED         - LTP hit the stop-loss band. A reduce / exit intent
                 is emitted.
PAUSED         - User-paused; no state transitions.
COMPLETED      - Final state after EXITED or STOPPED; no further
                 transitions.

Configuration (PovConfig)
-------------------------
  mode                "investing" | "short_term"
  qty_per_fire        Shares per fire in `investing` mode.
  risk_pct            Per-symbol risk as a fraction of available cash
                      (only used in `short_term` mode).
  touch_buffer_pct    Width of the touch band around trigger_level
                      (default 0.05 = 0.05%).
  ceiling_pct_above_trigger
                      How far ABOVE the trigger to consider "ceiling
                      reached" (default 1.0%).
  floor_pct_below_trigger
                      How far BELOW the trigger to consider "floor
                      reached" (default 1.0%).
  stop_loss_pct_below_trigger
                      Distance below trigger that triggers EXITED
                      (default 2.0%).
  pov_min_pct         Min participation % when in SLOW state.
  pov_max_pct         Max participation % when in ACCUMULATING state.

The state machine is **pure** - it takes (state, ltp, ts) and
returns the next state + a "trade intent" (buy / sell / nothing).
The dashboard layer is responsible for applying the intent to the
virtual ledger.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PovState(str, Enum):
    """The states a per-symbol POV can be in."""

    WATCHING = "WATCHING"
    MATCHED = "MATCHED"
    ACCUMULATING = "ACCUMULATING"
    SLOW = "SLOW"
    STOPPED = "STOPPED"
    EXITED = "EXITED"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


# States from which we can resume normal operation after a pause.
RESUMABLE_STATES = {PovState.WATCHING, PovState.MATCHED, PovState.ACCUMULATING, PovState.SLOW}


@dataclass
class PovConfig:
    """Per-symbol POV tuning knobs.

    Defaults are conservative and tuned for liquid NSE large caps.
    """

    mode: str = "short_term"  # "investing" | "short_term"
    qty_per_fire: int = 100
    risk_pct: float = 0.01  # 1% of cash per symbol (short_term only)
    touch_buffer_pct: float = 0.05
    ceiling_pct_above_trigger: float = 1.0
    floor_pct_below_trigger: float = 1.0
    stop_loss_pct_below_trigger: float = 2.0
    pov_min_pct: float = 5.0
    pov_max_pct: float = 25.0

    def __post_init__(self) -> None:
        m = (self.mode or "short_term").lower()
        if m not in {"investing", "short_term"}:
            raise ValueError(f"mode must be investing|short_term; got {self.mode!r}")
        self.mode = m
        if self.qty_per_fire < 0:
            raise ValueError("qty_per_fire must be >= 0")
        if not 0 < self.risk_pct <= 1:
            raise ValueError("risk_pct must be in (0, 1]")


@dataclass
class PovTickResult:
    """The outcome of one state-machine tick."""

    state: PovState
    prev_state: PovState
    intent: str  # "BUY" | "SELL" | "REDUCE" | "" (no action)
    qty: int  # Suggested quantity for the intent (0 if no action)
    pov_pct: float  # Current POV rate (informational)
    reason: str  # Human-readable explanation
    ts: float  # Unix timestamp of the tick


@dataclass
class PovStateMachine:
    """Per-symbol POV state machine. Pure: tick() is deterministic
    apart from timestamp capture.
    """

    symbol: str
    exchange: str
    config: PovConfig
    state: PovState = PovState.WATCHING
    trigger_level: Optional[float] = None
    pattern: Optional[str] = None  # "RRG" | "GRR" (None until matched)
    action: str = "BUY"  # action implied by pattern: RRG->BUY, GRR->SELL
    avg_price: Optional[float] = None
    shares_held: int = 0
    fire_count: int = 0
    last_tick_ts: float = 0.0
    last_intent_ts: float = 0.0
    history: List[PovTickResult] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def set_match(self, pattern: str, trigger_level: float) -> None:
        """Mark the symbol as having matched an OCL pattern.

        Transitions WATCHING -> MATCHED. The next tick() with an
        LTP in the touch band will move us to ACCUMULATING.
        """
        self.pattern = pattern
        self.trigger_level = float(trigger_level)
        self.action = "BUY" if pattern == "RRG" else "SELL"
        if self.state == PovState.WATCHING:
            self.state = PovState.MATCHED

    def reset(self) -> None:
        """Wipe the machine back to WATCHING (e.g. on pattern expiry)."""
        self.state = PovState.WATCHING
        self.trigger_level = None
        self.pattern = None
        self.fire_count = 0
        self.last_intent_ts = 0.0
        # NB: we deliberately keep avg_price and shares_held so the
        # position tracker can still close the position if needed.

    def pause(self) -> None:
        if self.state in RESUMABLE_STATES:
            self.state = PovState.PAUSED

    def resume(self) -> None:
        if self.state == PovState.PAUSED:
            self.state = PovState.WATCHING if self.trigger_level is None else PovState.MATCHED

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def tick(
        self,
        ltp: float,
        ts: float,
        *,
        available_cash: float = 0.0,
    ) -> PovTickResult:
        """Advance the state machine by one price tick.

        Args:
            ltp: last-traded-price right now.
            ts: monotonic timestamp (e.g. time.time()).
            available_cash: cash available for sizing (only used in
                short_term mode). Zero means "no budget".

        Returns:
            PovTickResult describing the new state + any intent.
        """
        self.last_tick_ts = ts
        prev = self.state

        # If paused, no-op.
        if prev == PovState.PAUSED:
            return PovTickResult(prev_state=prev, state=prev, intent="", qty=0, pov_pct=0.0, reason="paused", ts=ts)

        # No pattern yet -> stay in WATCHING.
        if self.trigger_level is None or self.pattern is None:
            return PovTickResult(prev_state=prev, state=PovState.WATCHING, intent="", qty=0, pov_pct=0.0, reason="no pattern", ts=ts)

        trigger = self.trigger_level
        cfg = self.config
        # Bullish (RRG): we want the LTP to DIP DOWN to trigger.
        # Bearish (GRR): we want the LTP to RALLY UP to trigger.
        if self.action == "BUY":
            upper = trigger * (1 + cfg.touch_buffer_pct / 100.0)
            lower = trigger * (1 - cfg.touch_buffer_pct / 100.0)
            ceiling = trigger * (1 + cfg.ceiling_pct_above_trigger / 100.0)
            floor = trigger * (1 - cfg.floor_pct_below_trigger / 100.0)
            stop_loss = trigger * (1 - cfg.stop_loss_pct_below_trigger / 100.0)
        else:  # SELL
            upper = trigger * (1 + cfg.touch_buffer_pct / 100.0)
            lower = trigger * (1 - cfg.touch_buffer_pct / 100.0)
            ceiling = trigger * (1 + cfg.ceiling_pct_above_trigger / 100.0)
            floor = trigger * (1 - cfg.floor_pct_below_trigger / 100.0)
            stop_loss = trigger * (1 - cfg.stop_loss_pct_below_trigger / 100.0)

        # LTP-driven state machine.
        new_state = prev
        intent = ""
        qty = 0
        pov_pct = 0.0
        reason = ""

        if prev in (PovState.STOPPED, PovState.EXITED, PovState.COMPLETED):
            # Terminal states - stay there.
            return PovTickResult(prev_state=prev, state=prev, intent="", qty=0, pov_pct=0.0, reason="terminal", ts=ts)

        if prev == PovState.WATCHING:
            # Move straight to MATCHED (set_match() should have done it
            # already, but in case tick() is called before set_match).
            new_state = PovState.MATCHED

        if prev in (PovState.MATCHED, PovState.ACCUMULATING, PovState.SLOW):
            # Check ceiling / SL first (they are terminal-ish and take
            # precedence over the touch-band logic), then fall through
            # to the touch-band branches.
            if self.action == "BUY":
                if ltp <= stop_loss and ltp > 0:
                    new_state = PovState.EXITED
                    intent = "SELL"
                    qty = self.shares_held
                    reason = f"SL hit: ltp {ltp:.2f} <= stop_loss {stop_loss:.2f}"
                    pov_pct = 0.0
                elif ltp >= ceiling:
                    new_state = PovState.STOPPED
                    reason = f"ceiling: ltp {ltp:.2f} >= ceiling {ceiling:.2f}"
                elif ltp <= upper:
                    # Touch band.
                    if ltp <= floor:
                        new_state = PovState.SLOW
                        pov_pct = cfg.pov_min_pct
                        reason = f"floor: ltp {ltp:.2f} <= floor {floor:.2f}; POV slow"
                        intent, qty = self._buy_intent(ltp, available_cash, slow=True)
                    else:
                        new_state = PovState.ACCUMULATING
                        pov_pct = cfg.pov_max_pct
                        reason = f"touch band: ltp {ltp:.2f} in [{lower:.2f}, {upper:.2f}]"
                        intent, qty = self._buy_intent(ltp, available_cash, slow=False)
                else:
                    # Between upper and ceiling: LTP is above the touch
                    # band but not yet at the ceiling. We stay MATCHED
                    # but no new buys happen.
                    new_state = PovState.MATCHED
                    reason = f"waiting: ltp {ltp:.2f} above touch band"
            elif self.action == "SELL":
                if ltp >= stop_loss and ltp > 0:
                    new_state = PovState.EXITED
                    intent = "BUY"
                    qty = self.shares_held
                    reason = f"SL hit: ltp {ltp:.2f} >= stop_loss {stop_loss:.2f}"
                    pov_pct = 0.0
                elif ltp <= floor:
                    new_state = PovState.SLOW
                    pov_pct = cfg.pov_min_pct
                    reason = f"floor: ltp {ltp:.2f} <= floor {floor:.2f}"
                elif ltp >= ceiling:
                    new_state = PovState.STOPPED
                    reason = f"ceiling: ltp {ltp:.2f} >= ceiling {ceiling:.2f}"
                elif ltp >= lower:
                    new_state = PovState.ACCUMULATING
                    pov_pct = cfg.pov_max_pct
                    reason = f"touch band: ltp {ltp:.2f} in [{lower:.2f}, {upper:.2f}]"
                else:
                    new_state = PovState.MATCHED
                    reason = f"waiting: ltp {ltp:.2f} below touch band"
            else:
                # Outside the touch band.
                new_state = PovState.MATCHED
                reason = f"waiting: ltp {ltp:.2f} not in touch band"

        # Update averages + counters if we just emitted a buy.
        if intent == "BUY" and qty > 0:
            if self.action == "BUY":
                new_total_cost = (self.avg_price or 0.0) * self.shares_held + ltp * qty
                self.shares_held += qty
                self.avg_price = new_total_cost / self.shares_held if self.shares_held else None
                self.fire_count += 1
                self.last_intent_ts = ts
        elif intent == "SELL" and qty > 0:
            self.shares_held = max(0, self.shares_held - qty)
            if self.shares_held == 0:
                self.avg_price = None
            self.fire_count += 1
            self.last_intent_ts = ts
            if self.action == "BUY" and new_state == PovState.EXITED:
                new_state = PovState.COMPLETED

        result = PovTickResult(
            prev_state=prev,
            state=new_state,
            intent=intent,
            qty=qty,
            pov_pct=pov_pct,
            reason=reason,
            ts=ts,
        )
        self.state = new_state
        self.history.append(result)
        # Cap history so a long-running dashboard doesn't grow memory
        # without bound. 500 ticks ~= 25 minutes at 3s/refresh.
        if len(self.history) > 500:
            self.history = self.history[-500:]
        return result

    # ------------------------------------------------------------------
    # Intent sizing
    # ------------------------------------------------------------------

    def _buy_intent(self, ltp: float, available_cash: float, slow: bool) -> tuple:
        """Return (intent, qty) for a buy tick.

        In `investing` mode: qty is the literal ``qty_per_fire``.
        In `short_term` mode: qty is the risk-managed size, computed
        as ``floor(available_cash * risk_pct / ltp)``. This caps the
        loss on a SL hit to ``risk_pct`` of cash, regardless of how
        many shares we buy.
        """
        if self.action != "BUY":
            # Short-selling not implemented in the dashboard yet.
            return ("", 0)
        cfg = self.config
        if cfg.mode == "investing":
            qty = int(cfg.qty_per_fire)
            if qty > 0:
                return ("BUY", qty)
            return ("", 0)
        # short_term
        if available_cash <= 0 or ltp <= 0:
            return ("", 0)
        budget = available_cash * cfg.risk_pct
        qty = int(budget // ltp)
        if slow:
            # When slow, the dashboard halves the rate (one trade
            # every other tick). We model that as halving the qty.
            qty = max(1, qty // 2)
        if qty > 0:
            return ("BUY", qty)
        return ("", 0)