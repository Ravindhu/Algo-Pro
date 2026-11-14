"""Data QA gate. Any ERROR stops the run before signals are made.

Flow stage: Data QA gate
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/ml/audit/quality.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

import numpy as np
import pandas as pd

REQUIRED = ["date", "symbol", "open", "high", "low", "close", "volume"]
STALE_RUN = 5
MAX_OVERNIGHT_MOVE = 0.30


@dataclass
class QAReport:
    issues: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(level == "ERROR" for level, _ in self.issues)


def check_bars(bars: pd.DataFrame, actions: pd.DataFrame = None) -> QAReport:
    """actions: optional columns symbol, date. Overnight moves on those dates are expected."""
    rep = QAReport()
    missing = [c for c in REQUIRED if c not in bars.columns]
    if missing:
        rep.issues.append(("ERROR", f"missing columns: {missing}"))
        return rep
    if bars.duplicated(["date", "symbol"]).any():
        rep.issues.append(("ERROR", "duplicate date+symbol rows"))
    if bars[["open", "high", "low", "close"]].isna().any().any():
        rep.issues.append(("ERROR", "NaN prices"))
    if (bars[["open", "high", "low", "close"]] <= 0).any().any():
        rep.issues.append(("ERROR", "non-positive prices"))
    if (bars["high"] < bars["low"]).any():
        rep.issues.append(("ERROR", "high below low"))
    bad_range = (bars["close"] > bars["high"]) | (bars["close"] < bars["low"])
    if bad_range.any():
        rep.issues.append(("ERROR", "close outside high/low"))

    df = bars.sort_values(["symbol", "date"])
    for sym, g in df.groupby("symbol"):
        closes = g["close"].to_numpy()
        run = 1
        for i in range(1, len(closes)):
            run = run + 1 if closes[i] == closes[i - 1] else 1
            if run == STALE_RUN:
                rep.issues.append(("WARNING", f"{sym}: stale close for {STALE_RUN}+ bars"))
                break
        move = np.abs(g["close"].pct_change().to_numpy())
        dates = g["date"].to_numpy()
        for i in np.where(move > MAX_OVERNIGHT_MOVE)[0]:
            if actions is None or not ((actions["symbol"] == sym) & (actions["date"] == dates[i])).any():
                rep.issues.append(("ERROR", f"{sym}: unexplained move > {MAX_OVERNIGHT_MOVE:.0%} on {dates[i]}"))
    if (bars["volume"] == 0).any():
        rep.issues.append(("WARNING", "zero-volume bars present"))
    return rep
