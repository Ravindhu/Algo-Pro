"""OCL sell-failure blueprints, BUY side. Ported from Algo-Lab (framework/strategies/ocl_sell_fail.py).

Flow stage: Strategy setup and entry (pure rules; no broker, no I/O)
Status: PHASE 2 IMPLEMENTED (paper only, ported 2026-11-08; not live). Handoff store is passed in by the caller (no file read here).

The core is a failed short: C1 any colour, C2 GREEN (its open is the conservative SELL level),
C3 RED (its open is the aggressive SELL level), bodies above min_body_pct on C2 and C3. A later
closed candle that CLOSES ABOVE the aggressive level breaks the pattern. The levels then flip to
BUY levels, and the entry waits for a retest from above.

TOP   (2 key levels): tier 1 = broken C3 open, tier 2 = C2 open. Tier 2 arms when tier 1's stop is hit.
BOTTOM (1 key level): the open of the LOWEST swing candle in the core..failure window; stop = its low.
BOTTOM runs only after TOP has exhausted the same core (handoff). This keeps the two paired per symbol.

Freshness: no candle between C3 and the break may have already traded at or above the sell levels.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from quant.strategies.bars import Bar, closed, last_bars
from quant.strategies.base import MultiTimeframeStrategy
from quant.strategies.indicators import atr as atr_series
from quant.strategies.indicators import hm_bullish
from quant.strategies.ocl import EXIT_AUTO, EXIT_DYNAMIC, EXIT_FIXED, OclParams

log = logging.getLogger("quant.strategies.ocl_sell_fail")


@dataclass(frozen=True)
class SellFailParams(OclParams):
    max_window: int = 30                 # HTF bars back to scan for the failed-short core

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.max_window < 5:
            raise ValueError("max_window must be >= 5 HTF bars")


class Handoff:
    """Records which failed-short cores TOP has finished with. BOTTOM only arms a core TOP exhausted.

    The caller owns this object and shares one instance between TOP and BOTTOM for one symbol.
    Persistence (to a state file) is added by the scanner step, not here."""

    def __init__(self) -> None:
        self._marks: dict[tuple[str, str], bool] = {}

    def exhausted(self, symbol: str, key: str) -> bool:
        return self._marks.get((symbol, key), False)

    def mark_exhausted(self, symbol: str, key: str) -> None:
        if key:
            self._marks[(symbol, key)] = True

    def to_list(self) -> list:
        return [[sym, key] for (sym, key) in sorted(self._marks)]

    def restore_list(self, items: list) -> None:
        for sym, key in items:
            self._marks[(str(sym), str(key))] = True


def _sell_core(bars: list[Bar], p: SellFailParams) -> int | None:
    """Newest failed-short core: index i where bars[i-1] is GREEN and bars[i] is RED, both with bodies
    above min_body_pct. Scanned newest first so the freshest setup wins."""
    lo = max(2, len(bars) - p.max_window)
    for i in range(len(bars) - 1, lo - 1, -1):
        c2, c3 = bars[i - 1], bars[i]
        if not (c2.green and c3.red):
            continue
        for c in (c2, c3):
            rng = c.high - c.low
            if rng <= 0 or abs(c.close - c.open) / rng <= p.min_body_pct:
                break
        else:
            return i
    return None


def _break_index(bars: list[Bar], i: int) -> int | None:
    """First candle after the core whose CLOSE breaks the aggressive sell level (C3 open)."""
    break_level = bars[i].open
    cons = bars[i - 1].open
    top = max(break_level, cons)
    for k in range(i + 1, len(bars)):
        if bars[k].close > break_level:
            if all(bars[m].high < top for m in range(i + 1, k)):
                return k
            return None                  # stale: the level was already tested
    return None


def _break_setup(bars: list[Bar], p: SellFailParams) -> dict[str, Any] | None:
    i = _sell_core(bars, p)
    if i is None:
        return None
    k = _break_index(bars, i)
    if k is None:
        return None
    c2, c3, fail = bars[i - 1], bars[i], bars[k]
    return {
        "core_start": i - 2,
        "aggressive": c3.open,
        "conservative": c2.open,
        "fail_high": max(c2.high, c3.high, fail.high),
        "fail_low": min(c2.low, c3.low, fail.low),
        "fail_index": k,
        "key": "",                       # filled by the caller from the frame index
        "bars": bars,
    }


def _lowest_swing(bars: list[Bar], start: int, end: int) -> Bar:
    return min(bars[start:end + 1], key=lambda b: b.low)


class OclSellFailBase(MultiTimeframeStrategy):
    """Shared engine for TOP and BOTTOM. Subclasses set `blueprint` and `_levels`."""

    blueprint = "TOP"

    def __init__(self, symbol: str, htf: str = "15minute", ltf: str = "5minute",
                 handoff: Handoff | None = None, **params: Any) -> None:
        self.p = SellFailParams.from_dict(params)
        super().__init__(symbol, htf, ltf, **params)
        self.st = self._new_state()
        self._handoff = handoff if handoff is not None else Handoff()

    @staticmethod
    def _new_state() -> dict[str, Any]:
        return {"armed": False, "key": None, "htf_sl": None, "level": None,
                "level2": None, "aggressive": None, "conservative": None,
                "retries": 0, "in_trade": False, "used": []}

    def _levels(self, setup: dict[str, Any]) -> tuple[float, float, float, float]:
        """(trigger, level2, aggressive, conservative) for this blueprint."""
        raise NotImplementedError

    def state(self) -> dict[str, Any]:
        return dict(self.st, used=list(self.st["used"]))

    def restore(self, d: dict[str, Any]) -> None:
        known = set(self._new_state())
        self.st = self._new_state() | {k: v for k, v in d.items() if k in known}

    def signal_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, Any] | None:
        p = self.p
        htf = closed(frames[self.htf_tf])
        ltf = closed(frames[self.ltf_tf])
        if len(ltf) < max(3, p.atr_period + 2) or len(htf) < 3:
            return None
        st = self.st

        htf_last = last_bars(htf, 1)[0]
        if st["armed"] and st["htf_sl"] is not None and htf_last.close < st["htf_sl"]:
            log.info("[%s] HTF close %.2f below floor %.2f; setup invalidated",
                     self.symbol, htf_last.close, st["htf_sl"])
            self._retire()
        if st["in_trade"]:
            return None

        if not st["armed"]:
            bars = last_bars(htf, min(len(htf), p.max_window))
            setup = _break_setup(bars, p)
            if setup is None:
                return None
            key = str(htf.index[len(htf) - len(bars) + setup["core_start"]])
            if key in st["used"]:
                return None
            if p.hm_enabled and not hm_bullish([float(x) for x in htf["close"]],
                                               p.rsi_period, p.ema_period):
                return None
            level, level2, aggr, cons = self._levels({**setup, "key": key, "frame_bars": bars})
            st.update(armed=True, key=key, level=level, level2=level2,
                      aggressive=aggr, conservative=cons,
                      htf_sl=setup["fail_low"], retries=0)
            log.info("[%s %s] armed %s: level %.2f, tier2 %.2f, floor %.2f",
                     self.symbol, self.blueprint, key, level, level2, st["htf_sl"])

        last = last_bars(ltf, 1)[0]
        if last.low > st["level"] + p.touch_buffer:
            return None
        b1, b2, b3 = last_bars(ltf, 3)
        if not (b1.red and b2.red and b3.green):
            return None
        if p.hm_enabled and not hm_bullish([float(x) for x in ltf["close"]], p.rsi_period,
                                           p.ema_period, oversold=p.oversold):
            return None

        ref = last.close
        if ref <= st["htf_sl"]:
            return None
        highs = [float(x) for x in ltf["high"]]
        lows = [float(x) for x in ltf["low"]]
        closes = [float(x) for x in ltf["close"]]
        atr_v = atr_series(highs, lows, closes, period=p.atr_period)[-1]
        if atr_v is None or atr_v <= 0:
            return None
        broker_sl = st["htf_sl"] - p.disaster_stop_atr * atr_v
        if broker_sl >= ref:
            return None
        exit_mode = self._resolve_exit(ref)
        tp_dist = p.target_rr * (ref - st["htf_sl"]) if exit_mode == EXIT_FIXED else None
        return {
            "side": "BUY",
            "sl_price": broker_sl,
            "sl_dist": ref - broker_sl,
            "tp_dist": tp_dist,
            "htf_sl": st["htf_sl"],
            "target_rr": p.target_rr,
            "trigger": st["level"],
            "aggressive": st["aggressive"],
            "conservative": st["conservative"],
            "exit_mode": exit_mode,
            "entry_ref": ref,
            "retries": st["retries"],
            "atr": atr_v,
            "atr_period": p.atr_period,
            "protect": p.protection.to_dict(),
            "max_spread_atr": p.max_spread_atr,
            "blueprint": self.blueprint,
            "time": str(ltf.index[-1]),
        }

    def on_entry(self) -> None:
        self.st["in_trade"] = True

    def on_exit(self, reason: str, price: float) -> None:
        st = self.st
        st["in_trade"] = False
        if reason != "SL" or not st["armed"]:
            self._retire()
            return
        if self.blueprint == "TOP" and st["level2"] is not None and st["level"] == st["aggressive"]:
            st["level"] = st["level2"]               # tier 1 failed: arm tier 2
            st["retries"] += 1
            log.info("[%s] tier-1 stop-out; tier 2 armed at %.2f (retry %d)",
                     self.symbol, st["level"], st["retries"])
            return
        if st["retries"] < self.p.max_ltf_retries:
            st["level"] = min(st["level"], price)
            st["retries"] += 1
            log.info("[%s] stop-out; retry %d/%d at %.2f", self.symbol,
                     st["retries"], self.p.max_ltf_retries, st["level"])
            return
        self._retire()

    def _retire(self) -> None:
        st = self.st
        if st["key"] is not None and st["key"] not in st["used"]:
            st["used"].append(st["key"])
        st["armed"] = False

    def _resolve_exit(self, ref: float) -> str:
        if self.p.exit_mode != EXIT_AUTO:
            return self.p.exit_mode
        st = self.st
        if st["aggressive"] is not None and ref >= st["aggressive"] - 0.005 * abs(st["aggressive"]):
            return EXIT_FIXED
        if st["conservative"] is not None and ref <= st["conservative"] + 0.005 * abs(st["conservative"]):
            return EXIT_DYNAMIC
        return EXIT_FIXED


class OclSellFailTop(OclSellFailBase):
    """SELL_FAILURE_TOP: two key levels. Tier 1 = broken C3 open, tier 2 = C2 open."""

    name = "ocl_sell_fail_top"
    blueprint = "TOP"

    def _levels(self, setup: dict[str, Any]) -> tuple[float, float, float, float]:
        return setup["aggressive"], setup["conservative"], setup["aggressive"], setup["conservative"]

    def _retire(self) -> None:
        super()._retire()
        self._handoff.mark_exhausted(self.symbol, self.st.get("key") or "")


class OclSellFailBottom(OclSellFailBase):
    """SELL_FAILURE_BOTTOM: one key level, the open of the lowest swing candle. Runs only after TOP
    has exhausted the same failed-short core (handoff)."""

    name = "ocl_sell_fail_bottom"
    blueprint = "BOTTOM"

    def _levels(self, setup: dict[str, Any]) -> tuple[float, float, float, float]:
        bars = setup["frame_bars"]
        swing = _lowest_swing(bars, setup["core_start"], setup["fail_index"])
        return swing.open, swing.open, setup["aggressive"], swing.open

    def signal_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, Any] | None:
        htf = closed(frames[self.htf_tf])
        if len(htf) >= 3 and not self.st["armed"] and not self.st["in_trade"]:
            bars = last_bars(htf, min(len(htf), self.p.max_window))
            setup = _break_setup(bars, self.p)
            if setup is None:
                return None
            key = str(htf.index[len(htf) - len(bars) + setup["core_start"]])
            if not self._handoff.exhausted(self.symbol, key):
                return None
        return super().signal_frames(frames)


__all__ = ["OclSellFailTop", "OclSellFailBottom", "SellFailParams", "Handoff", "EXIT_AUTO"]
