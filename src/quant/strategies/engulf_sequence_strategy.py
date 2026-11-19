"""Engulfing strategy for the bot: MTF/HTF/LTF sequence on closed bars from MultiTimeframeBars.

Flow stage: Strategy
Status: PHASE 1 IMPLEMENTED (2026-11-19). Paper only (the runner refuses any other mode).
The bot calls signal_for(symbol, df, now). The plain signal(df) returns None, because this
strategy needs the three timeframes, not only the bot's bars.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from quant.strategies.owner_patterns import engulf_signal


class EngulfSequenceStrategy:
    name = "engulfing"

    def __init__(self, feed) -> None:
        self._feed = feed

    def signal(self, df) -> Optional[dict]:
        return None

    def signal_for(self, symbol: str, df, now: datetime) -> Optional[dict]:
        frames = self._feed.closed_frames(symbol, now)
        if frames is None:
            return None
        return engulf_signal(frames["mtf"], frames["htf"], frames["ltf"])
