"""Base class for the BUY strategies. Ported from Algo-Lab (framework/strategies/base.py, multi.py).

Flow stage: Strategy (signal only; no orders, no network, no file writes)
Status: PHASE 2 IMPLEMENTED (paper only, ported 2026-11-08; not live).

Rules for every strategy:
  - signal_frames() is pure. It reads frames and the strategy's own state. It returns a dict or None.
  - It only looks at CLOSED bars. The last row of each frame is still forming, so closed() drops it.
  - The caller saves and restores state() / restore(), and reports fills and exits (on_entry / on_exit).
  - A signal dict always has side "BUY".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from quant.strategies.bars import closed


class MultiTimeframeStrategy(ABC):
    """Subclasses set `name` and implement signal_frames(). Frames are keyed by interval name."""

    name: str = "strategy"

    def __init__(self, symbol: str, htf: str, ltf: str, **params: Any) -> None:
        if not symbol or not isinstance(symbol, str):
            raise ValueError("symbol must be a non-empty NSE symbol name")
        self.symbol = symbol
        self.htf_tf = htf
        self.ltf_tf = ltf
        self.timeframes = (htf, ltf)
        self.params = params

    @abstractmethod
    def signal_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, Any] | None:
        """Return the BUY trade dict for the newest closed LTF bar, or None."""

    def exit_check(self, frames: dict[str, pd.DataFrame]) -> bool:
        """True when the strategy wants an open trade closed (HTF floor breached). Default: never."""
        return False

    def on_entry(self) -> None:
        """Called by the caller after a fill."""

    def on_exit(self, reason: str, price: float) -> None:
        """Called by the caller after a close. reason: SL, TRAIL, TP, HTF, DISASTER, EOD."""

    def state(self) -> dict[str, Any]:
        return {}

    def restore(self, d: dict[str, Any]) -> None:
        """Load saved state. Unknown keys are ignored."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.symbol}, {self.htf_tf}/{self.ltf_tf}, {self.params})"


__all__ = ["MultiTimeframeStrategy", "closed"]
