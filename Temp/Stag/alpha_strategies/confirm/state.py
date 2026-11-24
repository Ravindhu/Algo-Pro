"""
state.py - confirmation state machine for keylevel reversal setups.

================================================================================
PURPOSE
================================================================================
Turns the OCL keylevel touch into an explicit two-stage gate:

    IDLE ──touch──> ARMED ──roc flip──> CONFIRMED ──> intent to slicer
      ^                │
      └──── reset ─────┘   (session end / operator stop / new pattern)

The point of the intermediate ARMED state is that a *touch* no longer trades
by itself. The setup must survive long enough for momentum to actually flip.

DESIGN DECISION (operator-confirmed)
------------------------------------
There is NO 30-bar discard timer. A setup stays ARMED until the ROC flip
arrives or the run ends. This is the operator's chosen behaviour: a level
touch is treated as a standing thesis, not a decaying one. If the operator
wants a bounded wait, ``wait_seconds`` (or the signal's own run duration)
provides it - the state machine itself never expires a setup.

Note this removes a rejection path, so the trade count will be HIGHER than a
30-bar-discard design would produce. The momentum confirmation is therefore
doing all of the filtering work; validate it on its own merits in the
backtest before trusting the count.

================================================================================
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# State constants - stable strings so logs and dashboards can grep them.
STATE_IDLE = "IDLE"
STATE_ARMED = "ARMED"
STATE_CONFIRMED = "CONFIRMED"

VALID_STATES = (STATE_IDLE, STATE_ARMED, STATE_CONFIRMED)


@dataclass
class ConfirmState:
    """Everything the confirmation layer knows about the current setup."""

    state: str = STATE_IDLE
    #: The touched keylevel price. None while IDLE.
    level: Optional[float] = None
    #: Trade side the setup is arming for (BUY on RRG, SELL on GRR).
    action: str = "BUY"
    #: Bar index at which the touch armed the setup.
    arm_idx: Optional[int] = None
    #: Bar index at which the ROC flip confirmed the setup.
    confirm_idx: Optional[int] = None
    #: z-score value at the confirming bar (for the intent meta / audit log).
    confirm_z: Optional[float] = None
    #: Number of bars elapsed between arm and confirm.
    bars_waited: Optional[int] = None
    #: Free-form diagnostics (touch VWAP, band position, etc.).
    meta: Dict[str, Any] = field(default_factory=dict)


class ConfirmMachine:
    """Explicit IDLE -> ARMED -> CONFIRMED machine for one signal run.

    Deliberately dumb: it holds state and exposes guarded transitions. All
    market-data access and all math stay in the caller, so this class is
    trivially testable without an SDK or a market feed.
    """

    def __init__(self) -> None:
        self.s = ConfirmState()

    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self.s.state

    def is_idle(self) -> bool:
        return self.s.state == STATE_IDLE

    def is_armed(self) -> bool:
        return self.s.state == STATE_ARMED

    def is_confirmed(self) -> bool:
        return self.s.state == STATE_CONFIRMED

    # ------------------------------------------------------------------
    def arm(
        self,
        *,
        level: float,
        action: str,
        bar_idx: int,
        meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Arm on a keylevel touch. Returns True if the state changed.

        Only a transition from IDLE arms a setup. A touch while already
        ARMED or CONFIRMED is ignored - this is the "first touch only"
        rule: a level consumed by a setup cannot re-arm itself.
        """
        if self.s.state != STATE_IDLE:
            return False
        self.s.state = STATE_ARMED
        self.s.level = float(level)
        self.s.action = str(action).upper()
        self.s.arm_idx = int(bar_idx)
        self.s.confirm_idx = None
        self.s.confirm_z = None
        self.s.bars_waited = None
        self.s.meta = dict(meta or {})
        return True

    # ------------------------------------------------------------------
    def confirm(self, *, z_value: float, bar_idx: int) -> bool:
        """Confirm the armed setup on a momentum flip. True if it fired.

        Only ARMED setups can confirm. A flip with no touch behind it is
        exactly the "mid-air signal" this design exists to reject.
        """
        if self.s.state != STATE_ARMED:
            return False
        self.s.state = STATE_CONFIRMED
        self.s.confirm_z = float(z_value)
        self.s.confirm_idx = int(bar_idx)
        self.s.bars_waited = (
            int(bar_idx) - int(self.s.arm_idx)
            if self.s.arm_idx is not None else None
        )
        return True

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Return to IDLE and drop the current setup entirely.

        Called after a fire, at session end, or when the operator stops the
        run. After a reset a *new* touch can arm a fresh setup.
        """
        self.s = ConfirmState()

    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """Serialisable view for logs, the dashboard and the intent meta."""
        return {
            "state": self.s.state,
            "level": self.s.level,
            "action": self.s.action,
            "arm_idx": self.s.arm_idx,
            "confirm_idx": self.s.confirm_idx,
            "confirm_z": self.s.confirm_z,
            "bars_waited": self.s.bars_waited,
            "meta": dict(self.s.meta),
        }