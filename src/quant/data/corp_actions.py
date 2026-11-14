"""Corporate actions: back-adjust prices before a split, bonus or dividend date.

Flow stage: Universe and actions
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/symbol_meta.py
"""
from __future__ import annotations

import pandas as pd


def back_adjust(bars: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """actions columns: symbol, date, factor (new shares per old share, e.g. 2.0 for a 1:1 bonus).

    Prices before the action date are divided by factor. Volume is multiplied by it.
    """
    out = bars.copy()
    for _, a in actions.iterrows():
        mask = (out["symbol"] == a["symbol"]) & (out["date"] < a["date"])
        for col in ("open", "high", "low", "close"):
            out.loc[mask, col] = out.loc[mask, col] / a["factor"]
        out.loc[mask, "volume"] = out.loc[mask, "volume"] * a["factor"]
    return out
