"""Sequence strategies for the bot: MTF/HTF/LTF sequence on closed bars from MultiTimeframeBars.

Flow stage: Strategy, Closed bars MTF/HTF/LTF, Engulf sequence, Strategy bot cycle
Status: PHASE 2 IMPLEMENTED (2026-11-19). Five strategies (see SEQUENCE_KINDS in owner_patterns). Paper only.
The bot calls signal_for(symbol, df, now). Trigger freshness: newest closed LTF bar only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from quant.strategies.owner_patterns import SEQUENCE_KINDS, sequence_signal


class SequenceStrategy:
    """One sequence strategy. `kind` picks the MTF setup (see owner_patterns.SEQUENCE_KINDS)."""

    def __init__(self, feed, name: str, kind: Optional[str] = None) -> None:
        if kind is None:
            kind = SEQUENCE_KINDS[name]
        self._feed = feed
        self.name = name
        self.kind = kind

    def signal(self, df) -> Optional[dict]:
        return None  # needs the three timeframes, so the bot calls signal_for instead

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
