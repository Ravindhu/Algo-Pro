"""Flow stage: Strategy (signal interface, separate from the Phase 5 weight interface).
Status: PHASE 2 IMPLEMENTED (ported or new, with tests)

Signal strategy base, copied from Algo-Lab and made quant-native (no forex imports).
Phase 5 strategies in src/quant/strategies/base.py return target weights; this package
returns trade signals for the 5-minute screened-stock bot. The two do not share code.

Rules for every signal strategy:
  - signal() is pure: it reads the DataFrame and returns a dict or None.
    No orders, no network, no file writes.
  - Only the last CLOSED bar counts. The last row of df is still forming;
    use df.iloc[-2]. The bot passes only closed bars.
  - A signal dict must contain "side" ("BUY" or "SELL").
  - For live trading it must also contain "qty", "sl" and "tp".
    The bot refuses to send an intent without them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

VALID_SIDES = ("BUY", "SELL")
LIVE_REQUIRED_KEYS = ("qty", "sl", "tp")


class SignalStrategy(ABC):
    """Subclass, set ``name``, implement ``signal()``."""

    name: str = "strategy"

    def __init__(self, symbol: str, timeframe: str = "15m",
                 count: int = 300, **params: Any):
        self.symbol = str(symbol).upper().strip()
        self.timeframe = str(timeframe).strip()
        self.count = int(count)
        self.params = dict(params)

    @abstractmethod
    def signal(self, df: pd.DataFrame) -> dict | None:
        """Return {"side": "BUY"|"SELL", ...} for the closed bar, or None."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.symbol}, {self.timeframe}, {self.params})"


def check_signal(sig: dict | None, live: bool = False) -> tuple[bool, str]:
    """Validate a signal dict against the contract.

    Returns (ok, reason). ``live=True`` also requires qty, sl and tp.
    """
    if sig is None:
        return False, "no signal"
    if not isinstance(sig, dict):
        return False, "signal must be a dict"
    side = str(sig.get("side", "")).upper()
    if side not in VALID_SIDES:
        return False, f"side must be BUY or SELL, got {side!r}"
    if live:
        missing = [k for k in LIVE_REQUIRED_KEYS if sig.get(k) in (None, "")]
        if missing:
            return False, f"live signal missing {missing}"
        if float(sig["qty"]) <= 0:
            return False, "qty must be > 0"
    return True, "ok"
