"""Signal: direction, stop distance and time limit from a factor score.

Flow stage: Signal
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/alpha/base.py
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: int          # +1 long, -1 short, 0 flat
    score: float
    stop_pct: float         # stop distance as a fraction of price
    time_limit_bars: int


def make_signal(symbol: str, score: float, daily_vol: float, threshold: float = 1.0,
                stop_vol_mult: float = 2.0, time_limit_bars: int = 20) -> Signal:
    direction = 1 if score >= threshold else 0  # long-only in this version
    return Signal(symbol, direction, float(score), stop_vol_mult * float(daily_vol), time_limit_bars)
