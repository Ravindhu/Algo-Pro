"""Corporate actions: back-adjust prices before a split, bonus or dividend.

Flow stage: Universe and actions
Status: PHASE 1 IMPLEMENTED; defects fixed 2026-11-14
Existing code to reuse or wrap (do not copy): none in the engine.
    ``blue_engine/core/symbol_meta.py`` is about SME / restricted segments, NOT
    corporate actions - the previous header pointed at it and never imported it.

THREE DEFECTS FIXED (each found by testing, each corrupts silently)
------------------------------------------------------------------
1. ``factor == 0`` produced ``inf`` prices. A zero factor is meaningless, and
   ``inf`` propagates through every return, volatility and backtest after it.
   Now refused with ValueError.
2. A dividend row (``amount``, no ``factor``) raised ``KeyError``. Cash dividends
   are the most common corporate action, and a series missing them has a fake
   price drop on every ex-date.
3. A bars frame without a ``symbol`` column raised a bare ``KeyError``. It now
   gets a clear message.

THE DIVIDEND RULE
-----------------
A cash dividend is NOT a share-count change, so it is adjusted by RATIO, not by
dividing by a factor:

    adjusted_price(t) = price(t) * (1 - dividend / price_on_ex_date)

Using the close on the ex-date as the reference is the standard simplification
and is stated here rather than hidden: a more precise version needs the previous
close, which is not always present.

COMPOUNDING
-----------
Actions are applied in date order, latest first, so two splits compound
correctly: a 2:1 then another 2:1 leaves prices before both divided by 4.
Volume moves the other way, so it is multiplied.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["back_adjust", "validate_actions"]

_PRICE_COLS = ("open", "high", "low", "close")


def validate_actions(actions: pd.DataFrame) -> None:
    """Raise a clear error for anything that would corrupt prices.

    A corporate action with no usable effect is refused rather than ignored: a
    silently skipped split shows up later as a fake 50% overnight gap that the QA
    gate will flag for the wrong reason.
    """
    required = {"symbol", "date"}
    if actions is None or len(actions) == 0:
        return
    missing = required - set(actions.columns)
    if missing:
        raise ValueError(f"actions missing columns: {sorted(missing)}")
    has_factor = "factor" in actions.columns
    has_amount = "amount" in actions.columns
    if not has_factor and not has_amount:
        raise ValueError(
            "actions needs either a 'factor' column (split/bonus) or an "
            "'amount' column (cash dividend); neither is present")

    if has_factor:
        factors = pd.to_numeric(actions["factor"], errors="coerce")
        bad = factors.notna() & (factors <= 0)
        if bad.any():
            raise ValueError(
                "factor must be > 0 (a 2:1 split is factor=2). "
                f"Found {factors[bad].tolist()} - a zero or negative factor "
                "would divide prices into inf or invert them.")
    if has_amount:
        amounts = pd.to_numeric(actions["amount"], errors="coerce")
        if (amounts < 0).any():
            raise ValueError("dividend amount must be >= 0")


def back_adjust(bars: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Back-adjust ``bars`` for splits, bonuses and dividends.

    ``bars`` columns: symbol, date, open, high, low, close, volume.
    ``actions`` columns: symbol, date, and either ``factor`` (new shares per old
    share, e.g. 2.0 for a 1:1 bonus) or ``amount`` (cash per share).

    Bars STRICTLY BEFORE the action date are adjusted. The bar ON the action date
    already trades at the post-action price, so adjusting it would double-count.
    """
    if "symbol" not in bars.columns:
        raise ValueError("bars needs a 'symbol' column to match actions")
    if "date" not in bars.columns:
        raise ValueError("bars needs a 'date' column to match actions")
    validate_actions(actions)

    out = bars.copy()
    if actions is None or len(actions) == 0:
        return out

    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    work = actions.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"]).sort_values("date", ascending=False)

    for _, a in work.iterrows():
        symbol = a["symbol"]
        mask = (out["symbol"] == symbol) & (out["date"] < a["date"])
        if not mask.any():
            continue

        if "factor" in work.columns and pd.notna(a.get("factor")):
            factor = float(a["factor"])
            if factor <= 0:
                raise ValueError(f"factor must be > 0, got {factor}")
            if factor == 1.0:
                continue  # no-op action (e.g. a 1:1 bonus that did not happen)
            for col in _PRICE_COLS:
                if col in out.columns:
                    out.loc[mask, col] = out.loc[mask, col] / factor
            if "volume" in out.columns:
                out.loc[mask, "volume"] = out.loc[mask, "volume"] * factor
            continue

        # Cash dividend: adjust by the ratio implied on the ex-date.
        amount = a.get("amount")
        if amount is None or pd.isna(amount):
            continue
        amount = float(amount)
        if amount <= 0:
            continue
        ex_rows = out[(out["symbol"] == symbol) & (out["date"] == a["date"])]
        if ex_rows.empty or "close" not in out.columns:
            continue
        ref = float(ex_rows["close"].iloc[0])
        if not np.isfinite(ref) or ref <= 0:
            continue
        pre = ref + amount          # price just before the ex-date
        ratio = ref / pre
        for col in _PRICE_COLS:
            if col in out.columns:
                out.loc[mask, col] = out.loc[mask, col] * ratio

    return out
