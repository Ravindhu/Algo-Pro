"""
confirm - multi-source confirmation layer for keylevel reversal setups.

Two independent confirmations sit between a keylevel *touch* and an actual
order going to a slicer:

    roc.py    z-scored Rate-of-Change  -> "did momentum actually flip?"
    state.py  IDLE/ARMED/CONFIRMED FSM -> "is this a real setup or a stray touch?"

They are deliberately split so each can be tested and validated alone. The
backtest harness measures each one's marginal contribution instead of
assuming both help (see the walk-forward step in the build plan).
"""
from __future__ import annotations

from .roc import (
    DEFAULT_ENTRY_Z,
    DEFAULT_ROC_PERIOD,
    DEFAULT_Z_WINDOW,
    find_flip,
    rate_of_change,
    zscored_roc,
)
from .state import (
    STATE_ARMED,
    STATE_CONFIRMED,
    STATE_IDLE,
    ConfirmMachine,
    ConfirmState,
)

__all__ = [
    "DEFAULT_ENTRY_Z",
    "DEFAULT_ROC_PERIOD",
    "DEFAULT_Z_WINDOW",
    "find_flip",
    "rate_of_change",
    "zscored_roc",
    "STATE_ARMED",
    "STATE_CONFIRMED",
    "STATE_IDLE",
    "ConfirmMachine",
    "ConfirmState",
]