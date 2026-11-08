"""OCL keylevel strategy, BUY side. Ported from Algo-Lab (framework/strategies/ocl.py).

Flow stage: Strategy setup and entry (pure rules; no broker, no I/O)
Status: PHASE 2 IMPLEMENTED (paper only, ported 2026-11-08; not live).

Naming: RRG = Red, Red, Green. Red closes below its open; green closes above it.

HTF (closed bars, newest last), candles C1..C4 (C4 newest):
  - C1 red, C2 red, C3 green.
  - C3 closes above C2's open (C3 break), or C4 closes above C2's open (C4 confirmation, if enabled).
  - Aggressive level   = C2 open (C3 break) or C3 close (C4 confirmation).
  - Conservative level = C3 open.
  - HTF floor = min(low C2, low C3, low confirmation candle).
  - HTF gate: RSI above EMA(RSI). No oversold cap on the HTF.
  - An HTF close below the floor ends the pattern.
LTF (closed bars):
  - Touch: last closed low <= trigger + touch buffer.
  - RRG on the last 3 closed LTF bars (if ltf_pattern_enabled).
  - LTF gate: RSI above EMA(RSI) AND RSI at or below oversold (default 30).
Exit mode AUTO: entry at the aggressive level -> FIXED (fixed-RR target); at the conservative
level -> DYNAMIC (no target; the ATR trail runs the trade).
Retry ladder: an LTF stop-out steps the trigger down to that bar's close and retries, up to
max_ltf_retries. Then the pattern is abandoned.

Deviations from the Algo-Lab port (both explicit):
  - touch_buffer is in price units (rupees). The forex pip conversion is removed.
  - DV-ATR trail not ported (Algo-Lab does not port it either). The paper config sets dvs_enabled false.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from quant.strategies.bars import Bar, closed, last_bars
from quant.strategies.base import MultiTimeframeStrategy
from quant.strategies.indicators import atr as atr_series
from quant.strategies.indicators import hm_bullish, hm_rsi_series
from quant.strategies.protect import Protection

log = logging.getLogger("quant.strategies.ocl")

AGGRESSIVE = "AGGRESSIVE"
CONSERVATIVE = "CONSERVATIVE"
EXIT_AUTO, EXIT_FIXED, EXIT_DYNAMIC = "AUTO", "FIXED", "DYNAMIC"


@dataclass(frozen=True)
class OclParams:
    rsi_period: int = 9
    ema_period: int = 3
    oversold: float = 30.0
    hm_enabled: bool = True
    ltf_pattern_enabled: bool = True
    allow_c4_confirmation: bool = True
    trigger_level_mode: str = CONSERVATIVE
    touch_buffer: float = 0.0            # price units (rupees)
    target_rr: float = 3.0
    exit_mode: str = EXIT_AUTO
    max_ltf_retries: int = 3
    atr_period: int = 14
    lookback: int = 120                  # closed bars per frame; enough for RSI, EMA and ATR warm-up
    disaster_stop_atr: float = 1.0       # broker stop = HTF floor - this * ATR (safety, not in the original)
    max_spread_atr: float = 0.25         # checked by the scanner using live quotes
    atr_trail_enabled: bool = True
    atr_multiplier: float = 2.0
    breakeven_rr: float = 1.5
    round_trip_cost_pct: float = 0.20
    min_body_pct: float = 0.5            # body share of range (sell-fail and engulfing setups)
    fib_pct: float = 0.886               # engulfing setup only

    def __post_init__(self) -> None:
        if self.trigger_level_mode not in (CONSERVATIVE, AGGRESSIVE):
            raise ValueError("trigger_level_mode must be CONSERVATIVE or AGGRESSIVE")
        if self.exit_mode not in (EXIT_AUTO, EXIT_FIXED, EXIT_DYNAMIC):
            raise ValueError("exit_mode must be AUTO, FIXED or DYNAMIC")
        if self.rsi_period < 2 or self.ema_period < 1 or self.atr_period < 2:
            raise ValueError("rsi_period >= 2, ema_period >= 1, atr_period >= 2")
        if not 0.0 <= self.oversold <= 100.0:
            raise ValueError("oversold must be within [0, 100]")
        if min(self.touch_buffer, self.max_spread_atr, self.disaster_stop_atr) < 0:
            raise ValueError("touch_buffer, max_spread_atr and disaster_stop_atr must be >= 0")
        if self.target_rr <= 0:
            raise ValueError("target_rr must be > 0")
        if self.max_ltf_retries < 0:
            raise ValueError("max_ltf_retries must be >= 0")
        if self.lookback < 30:
            raise ValueError("lookback must be >= 30 bars")
        if not 0.0 <= self.min_body_pct < 1.0:
            raise ValueError("min_body_pct must be within [0, 1)")
        if not 0.0 < self.fib_pct < 1.0:
            raise ValueError("fib_pct must be within (0, 1)")
        self.protection  # runs Protection's own validation

    @property
    def protection(self) -> Protection:
        return Protection(atr_trail_enabled=self.atr_trail_enabled, atr_multiplier=self.atr_multiplier,
                          breakeven_rr=self.breakeven_rr, round_trip_cost_pct=self.round_trip_cost_pct)

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "OclParams":
        known = set(cls.__dataclass_fields__)
        unknown = set(cfg) - known
        if unknown:
            raise ValueError(f"unknown OCL parameter(s): {sorted(unknown)}")
        return cls(**cfg)


def htf_setup(htf_closed: pd.DataFrame, p: OclParams) -> dict[str, Any] | None:
    """OCL pattern on CLOSED HTF bars (newest last). Returns levels, floor and the confirm label."""
    need = 4 if p.allow_c4_confirmation else 3
    if len(htf_closed) < need:
        return None
    bars = last_bars(htf_closed, need)
    if p.allow_c4_confirmation:
        c1, c2, c3, c4 = bars
    else:
        (c1, c2, c3), c4 = bars, None
    if not (c1.red and c2.red and c3.green):
        return None
    if c3.close > c2.open:
        confirm, name = c3, "C3"
    elif c4 is not None and c4.close > c2.open:
        confirm, name = c4, "C4"
    else:
        return None
    return {
        "aggressive": c2.open if name == "C3" else c3.close,
        "conservative": c3.open,
        "htf_sl": min(c2.low, c3.low, confirm.low),
        "confirm_bar": name,
    }


@dataclass
class SetupState:
    """What the strategy remembers between cycles. Plain types, so it saves to JSON."""
    armed: bool = False
    key: str | None = None               # identity of the armed pattern: time of its first candle
    htf_sl: float | None = None
    level: float | None = None           # current trigger; steps DOWN after each stop-out
    aggressive: float | None = None
    conservative: float | None = None
    confirm_bar: str | None = None
    retries: int = 0
    in_trade: bool = False
    used: list[str] = field(default_factory=list)


class OclKeylevel(MultiTimeframeStrategy):
    name = "ocl"

    def __init__(self, symbol: str, htf: str = "15minute", ltf: str = "5minute", **params: Any) -> None:
        self.p = OclParams.from_dict(params)
        super().__init__(symbol, htf, ltf, **params)
        self.st = SetupState()

    # ---- state persistence (caller saves and restores; the strategy never touches disk) ----
    def state(self) -> dict[str, Any]:
        return dict(self.st.__dict__, used=list(self.st.used))

    def restore(self, d: dict[str, Any]) -> None:
        known = set(SetupState.__dataclass_fields__)
        self.st = SetupState(**{k: v for k, v in d.items() if k in known})

    # ---- signal ----
    def signal_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, Any] | None:
        p = self.p
        htf = closed(frames[self.htf_tf])
        ltf = closed(frames[self.ltf_tf])
        need_ltf = max(3 if p.ltf_pattern_enabled else 1, p.atr_period + 2)
        if len(ltf) < need_ltf or len(htf) < 1:
            return None

        st = self.st
        htf_last = last_bars(htf, 1)[0]
        if st.armed and st.htf_sl is not None and htf_last.close < st.htf_sl:
            log.info("[%s] HTF close %.2f below floor %.2f; pattern invalidated",
                     self.symbol, htf_last.close, st.htf_sl)
            if st.key is not None and st.key not in st.used:
                st.used.append(st.key)              # dead until a NEW HTF pattern forms
            st.armed = False
        if st.in_trade:
            return None

        if not st.armed:
            setup = self._htf_setup(htf)
            if setup is None:
                return None
            key = setup["key"]
            if key in st.used:
                return None
            st.armed, st.key = True, key
            st.htf_sl = setup["htf_sl"]
            st.aggressive, st.conservative = setup["aggressive"], setup["conservative"]
            st.confirm_bar = setup["confirm_bar"]
            st.level = st.aggressive if p.trigger_level_mode == AGGRESSIVE else st.conservative
            st.retries = 0
            log.info("[%s] armed %s: level %.2f, floor %.2f", self.symbol, key, st.level, st.htf_sl)

        last = last_bars(ltf, 1)[0]
        if last.low > st.level + p.touch_buffer:
            return None
        if p.ltf_pattern_enabled:
            b1, b2, b3 = last_bars(ltf, 3)
            if not (b1.red and b2.red and b3.green):
                return None
        if p.hm_enabled and not hm_bullish([float(x) for x in ltf["close"]], p.rsi_period,
                                           p.ema_period, oversold=p.oversold):
            return None

        ref = last.close
        if ref <= st.htf_sl:
            return None
        atr_v, rsi_v = self._ltf_atr_rsi(ltf)
        if atr_v is None or atr_v <= 0:
            return None
        broker_sl = st.htf_sl - p.disaster_stop_atr * atr_v
        if broker_sl >= ref:
            return None
        exit_mode = self._resolve_exit(ref)
        tp_dist = p.target_rr * (ref - st.htf_sl) if exit_mode == EXIT_FIXED else None
        return {
            "side": "BUY",
            "sl_price": broker_sl,               # broker stop level
            "sl_dist": ref - broker_sl,
            "tp_dist": tp_dist,                  # distance above entry; None for DYNAMIC
            "htf_sl": st.htf_sl,
            "target_rr": p.target_rr,
            "trigger": st.level,
            "aggressive": st.aggressive,
            "conservative": st.conservative,
            "exit_mode": exit_mode,
            "entry_ref": ref,
            "confirm_bar": st.confirm_bar,
            "retries": st.retries,
            "atr": atr_v,
            "atr_period": p.atr_period,
            "rsi": rsi_v,
            "protect": p.protection.to_dict(),
            "max_spread_atr": p.max_spread_atr,
            "time": str(ltf.index[-1]),
        }

    def _htf_setup(self, htf: pd.DataFrame) -> dict[str, Any] | None:
        """HTF pattern plus the HM gate. Subclasses replace this; the LTF entry model stays the same."""
        p = self.p
        setup = htf_setup(htf, p)
        if setup is None:
            return None
        if p.hm_enabled and not hm_bullish([float(x) for x in htf["close"]], p.rsi_period, p.ema_period):
            return None
        need = 4 if p.allow_c4_confirmation else 3
        return {**setup, "key": str(htf.index[-need])}

    def exit_check(self, frames: dict[str, pd.DataFrame]) -> bool:
        """True when the last closed HTF bar closed below the floor of an open trade."""
        if not self.st.in_trade or self.st.htf_sl is None:
            return False
        htf = closed(frames[self.htf_tf])
        return len(htf) > 0 and last_bars(htf, 1)[0].close < self.st.htf_sl

    def on_entry(self) -> None:
        self.st.in_trade = True

    def on_exit(self, reason: str, price: float) -> None:
        """reason: SL, TRAIL, TP, HTF, DISASTER, EOD. For SL, price is the bar close that broke the stop."""
        st = self.st
        st.in_trade = False
        if reason == "SL" and st.armed and st.retries < self.p.max_ltf_retries:
            st.level = min(st.level, price)
            st.retries += 1
            log.info("[%s] LTF stop-out; retry %d/%d at %.2f", self.symbol,
                     st.retries, self.p.max_ltf_retries, st.level)
            return
        if st.key is not None and st.key not in st.used:
            st.used.append(st.key)
        st.armed = False

    # ---- helpers ----
    def _resolve_exit(self, ref: float) -> str:
        if self.p.exit_mode != EXIT_AUTO:
            return self.p.exit_mode
        st = self.st
        if ref >= st.aggressive - 0.005 * abs(st.aggressive):
            return EXIT_FIXED
        if ref <= st.conservative + 0.005 * abs(st.conservative):
            return EXIT_DYNAMIC
        return EXIT_FIXED

    def _ltf_atr_rsi(self, ltf: pd.DataFrame) -> tuple[float | None, float | None]:
        highs = [float(x) for x in ltf["high"]]
        lows = [float(x) for x in ltf["low"]]
        closes = [float(x) for x in ltf["close"]]
        a = atr_series(highs, lows, closes, period=self.p.atr_period)[-1]
        r = hm_rsi_series(closes, rsi_period=self.p.rsi_period)[-1]
        return a, (None if np.isnan(r) else float(r))


__all__ = ["OclKeylevel", "OclParams", "SetupState", "htf_setup", "Bar"]
