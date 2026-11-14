"""Exit manager: stop, trailing stop, time limit. Pure functions, state passed in.

Flow stage: Exit manager
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/exit_engine.py
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Tuple


@dataclass(frozen=True)
class ExitState:
    entry: float
    side: int                 # +1 long, -1 short
    stop_pct: float
    trail_pct: float
    time_limit_bars: int
    bars_held: int = 0
    peak: float = 0.0         # best price since entry, in the favourable direction

    def __post_init__(self):
        if self.peak == 0.0:
            object.__setattr__(self, "peak", self.entry)


def check_exit(state: ExitState, price: float) -> Tuple[Optional[str], ExitState]:
    """Returns (reason or None, updated state). Updates peak and bars held on every bar."""
    peak = max(state.peak, price) if state.side > 0 else min(state.peak, price)
    held = state.bars_held + 1
    new = replace(state, peak=peak, bars_held=held)
    if state.side > 0:
        if price <= state.entry * (1 - state.stop_pct):
            return "stop", new
        if price <= peak * (1 - state.trail_pct):
            return "trailing", new
    else:
        if price >= state.entry * (1 + state.stop_pct):
            return "stop", new
        if price >= peak * (1 + state.trail_pct):
            return "trailing", new
    if held >= state.time_limit_bars:
        return "time", new
    return None, new
