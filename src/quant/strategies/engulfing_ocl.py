"""Engulfing HTF setup with the OCL lower-timeframe entry model, BUY side.
Ported from Algo-Lab (framework/strategies/engulfing_ocl.py).

Flow stage: Strategy setup and entry (pure rules; no broker, no I/O)
Status: PHASE 2 IMPLEMENTED (paper only, ported 2026-11-08; not live).

HTF (closed bars): a bullish engulfing on the newest closed bar, both candle bodies above min_body_pct
of their range, and the Hilega-Milega gate (RSI above EMA(RSI)) on that bar. The setup arms one level:
the fib level inside the engulfing candle (fib_pct, default 88.6%). The floor is the engulfing candle's low.

LTF (closed bars): the OCL entry model unchanged (touch, RRG, LTF HM gate with oversold cap,
retry ladder, protection rules). Only the HTF setup differs from OclKeylevel.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from quant.strategies.engulfing import engulfing, fib_entry_level
from quant.strategies.indicators import hm_direction
from quant.strategies.ocl import OclKeylevel


class EngulfingOcl(OclKeylevel):
    name = "engulfing_ocl"

    def _htf_setup(self, htf: pd.DataFrame) -> dict[str, Any] | None:
        p = self.p
        if len(htf) < 2:
            return None
        direction = int(engulfing(htf, p.min_body_pct).iloc[-1])
        if direction <= 0:                          # BUY side only
            return None
        closes = [float(x) for x in htf["close"]]
        if p.hm_enabled and int(hm_direction(closes, p.rsi_period, p.ema_period)[-1]) != 1:
            return None
        high, low = float(htf["high"].iloc[-1]), float(htf["low"].iloc[-1])
        level = fib_entry_level(high, low, 1, p.fib_pct)
        return {"htf_sl": low, "aggressive": level, "conservative": level,
                "confirm_bar": "ENG", "key": str(htf.index[-1])}
