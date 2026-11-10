"""Flow stage: Strategy (signal package).
Status: PHASE 2 IMPLEMENTED (ported or new, with tests)

How to add a strategy:
  1. Copy this file to src/quant/strategies/signals/<your_name>.py.
  2. Rename the class and set ``name`` to a unique key.
  3. Put the rule in ``signal()``. Use only closed bars (df.iloc[-2]).
  4. Return {"side", "qty", "sl", "tp", ...} or None.
  5. Enable it per run in config/strategies/<your_name>.yaml. Default is off.

This sample never trades: it always returns None.
"""
from __future__ import annotations

import pandas as pd

from .base import SignalStrategy


class SampleStrategy(SignalStrategy):
    """Template. Returns no signal."""

    name = "sample"

    def signal(self, df: pd.DataFrame) -> dict | None:
        if df is None or len(df) < 2:
            return None
        # Rule goes here. df.iloc[-1] is still forming; use df.iloc[-2].
        return None
