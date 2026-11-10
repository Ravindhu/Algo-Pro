"""Flow stage: Strategy (signal confirmation state machine).
Status: PORTED COPY of Blue_engine/alpha/confirm/state.py. Tests: src/quant/tests/test_signals_roc_state.py.

Confirmation state machine for keylevel reversal setups.

    IDLE --touch--> ARMED --roc flip--> CONFIRMED --> intent to slicer
      ^                |
      +---- reset -----+   (session end / operator stop / new pattern)

A touch does not trade by itself. The setup must survive until momentum flips.
There is NO bar-count discard timer: a setup stays ARMED until the flip arrives
or the run ends (operator decision carried over from Blue_engine).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

STATE_IDLE = "IDLE"
STATE_ARMED = "ARMED"
STATE_CONFIRMED = "CONFIRMED"

VALID_STATES = (STATE_IDLE, STATE_ARMED, STATE_CONFIRMED)


@dataclass
class ConfirmState:
    """Everything the confirmation layer knows about the current setup."""

    state: str = STATE_IDLE
    level: Optional[float] = None
    action: str = "BUY"
    arm_idx: Optional[int] = None
    confirm_idx: Optional[int] = None
    confirm_z: Optional[float] = None
    bars_waited: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)


class ConfirmMachine:
    """Explicit IDLE -> ARMED -> CONFIRMED machine for one signal run.

    Holds state and exposes guarded transitions. Market data and math stay in
    the caller, so this class is testable without a feed.
    """

    def __init__(self) -> None:
        self.s = ConfirmState()

    @property
    def state(self) -> str:
        return self.s.state

    def is_idle(self) -> bool:
        return self.s.state == STATE_IDLE

    def is_armed(self) -> bool:
        return self.s.state == STATE_ARMED

    def is_confirmed(self) -> bool:
        return self.s.state == STATE_CONFIRMED

    def arm(
        self,
        *,
        level: float,
        action: str,
        bar_idx: int,
        meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Arm on a keylevel touch. True if the state changed.

        Only IDLE arms. A touch while ARMED or CONFIRMED is ignored ("first
        touch only": a level consumed by a setup cannot re-arm itself).
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

    def confirm(self, *, z_value: float, bar_idx: int) -> bool:
        """Confirm the armed setup on a momentum flip. True if it fired.

        Only ARMED setups can confirm. A flip with no touch behind it is rejected.
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

    def reset(self) -> None:
        """Return to IDLE and drop the current setup. A new touch may re-arm."""
        self.s = ConfirmState()

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
