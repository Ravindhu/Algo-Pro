"""Sequence strategies for the bot: MTF/HTF/LTF sequence on closed bars from MultiTimeframeBars.

Flow stage: Strategy, Closed bars MTF/HTF/LTF, Engulf sequence, Strategy bot cycle
Status: PHASE 2 IMPLEMENTED (2026-11-19). Five strategies (see SEQUENCE_KINDS in owner_patterns). Paper only.
The bot calls cycle_for(symbol, now) for every sequence strategy (is_cycle = True): one position per HTF cycle.
signal_for() is the older single-trigger path; the bot no longer calls it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from quant.indicators.indicators import atr as atr_series
from quant.strategies.cycle import CycleResult, run_cycles
from quant.strategies.owner_patterns import SEQUENCE_KINDS, sequence_signal


@dataclass
class CycleView:
    """Everything the bot needs for one symbol: all HTF cycles, the newest LTF bar time and ATR."""
    results: List[CycleResult]
    newest_ltf_time: object
    atr: Optional[float]


class SequenceStrategy:
    """One sequence strategy. `kind` picks the MTF setup (see owner_patterns.SEQUENCE_KINDS)."""

    is_cycle = True  # the bot runs the cycle rules (owner 2026-11-19), not a single trigger

    def __init__(self, feed, name: str, kind: Optional[str] = None, target_r: Optional[float] = None) -> None:
        if kind is None:
            kind = SEQUENCE_KINDS[name]
        self._feed = feed
        self.name = name
        self.kind = kind
        self.target_r = target_r  # None = target_pending (owner: backtested N not set yet)

    def signal(self, df) -> Optional[dict]:
        return None  # needs the three timeframes, so the bot calls signal_for instead

    def cycle_for(self, symbol: str, now: datetime) -> Optional[CycleView]:
        frames = self._feed.closed_frames(symbol, now)
        if frames is None:
            return None
        mtf, htf, ltf = frames["mtf"], frames["htf"], frames["ltf"]
        results = run_cycles(self.kind, mtf, htf, ltf, target_r=self.target_r)
        if not results or len(ltf) == 0:
            return None
        atr_val = None
        if len(ltf) >= 15:
            highs = ltf["high"].to_numpy(dtype=float).tolist()
            lows = ltf["low"].to_numpy(dtype=float).tolist()
            closes = ltf["close"].to_numpy(dtype=float).tolist()
            atr_val = atr_series(highs, lows, closes, period=14)[-1]
        return CycleView(results=results, newest_ltf_time=ltf.index[-1],
                         atr=float(atr_val) if atr_val is not None and atr_val > 0 else None)

    def signal_for(self, symbol: str, df, now: datetime) -> Optional[dict]:
        frames = self._feed.closed_frames(symbol, now)
        if frames is None:
            return None
        return sequence_signal(self.kind, frames["mtf"], frames["htf"], frames["ltf"],
                               name=self.name, fresh=True)


class EngulfSequenceStrategy(SequenceStrategy):
    """The Engulfing strategy (kept as a named class)."""

    def __init__(self, feed) -> None:
        super().__init__(feed, "engulfing", "egpattern_open")
