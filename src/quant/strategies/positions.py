"""Paper position manager: exits for investment trades with trailing stop and breakeven, as in Algo-Lab.

Flow stage: Exit manager (paper)
Status: PHASE 2 IMPLEMENTED (paper only, 2026-11-08; not live).

No simulated exit scenarios. Each open trade is managed by the same rules as Algo-Lab
(quant.strategies.protect.Trade), once per NEW closed LTF bar:
  1. HTF floor: the strategy's exit_check() says the HTF closed below the floor      -> "HTF"
  2. Intrabar (this bar's high and low):
       low <= broker disaster stop                                                  -> "DISASTER"
       high >= fixed target (FIXED exits only)                                      -> "TP"
  3. Close rules: close below the stop (initial stop or breakeven stop)              -> "SL"
       close below the ATR trail                                                    -> "TRAIL"
     Breakeven and the ATR trail advance on every bar that does not exit.
Paper fill prices: DISASTER at min(open, stop); TP at max(open, target); SL, TRAIL, HTF at the bar close.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Set

import pandas as pd

from quant.strategies.bars import closed, last_bars
from quant.strategies.indicators import atr as atr_series
from quant.strategies.protect import Protection, Trade


@dataclass
class Position:
    key: str
    strategy: Any                       # the strategy object (exit_check, on_exit, p)
    symbol: str
    qty: int
    trade: Trade
    protection: Protection
    atr_period: int
    ltf_tf: str
    entry_time: str
    last_bar: str                       # time of the last closed LTF bar already processed


class PaperPositionManager:
    def __init__(self, audit: Any) -> None:
        self._audit = audit
        self._open: Dict[str, Position] = {}
        self.closed_trades: List[Dict[str, Any]] = []

    # ---- queries used by the risk gate and the sizer ----
    def open_positions(self) -> Dict[str, Position]:
        return dict(self._open)

    def open_notional(self) -> float:
        return float(sum(p.qty * p.trade.entry for p in self._open.values()))

    def open_symbols(self) -> Set[str]:
        return {p.symbol for p in self._open.values()}

    # ---- lifecycle ----
    def open(self, *, key: str, strategy: Any, symbol: str, qty: int, sig: Mapping[str, Any],
             bar_time: str) -> Position:
        """Register a paper entry. The signal bar itself is not processed again for exits."""
        if key in self._open:
            raise ValueError(f"position already open for {key}")
        if qty < 1:
            raise ValueError("position qty must be at least 1")
        protection = strategy.p.protection
        tp = float(sig["entry_ref"]) + float(sig["tp_dist"]) if sig.get("tp_dist") else None
        trade = Trade.open(entry=float(sig["entry_ref"]), trigger=float(sig["trigger"]),
                           htf_sl=float(sig["htf_sl"]), broker_sl=float(sig["sl_price"]),
                           tp=tp, p=protection)
        pos = Position(key=key, strategy=strategy, symbol=symbol, qty=int(qty), trade=trade,
                       protection=protection, atr_period=int(strategy.p.atr_period),
                       ltf_tf=strategy.ltf_tf, entry_time=str(bar_time), last_bar=str(bar_time))
        self._open[key] = pos
        self._audit.append("position_open", {"key": key, "symbol": symbol, "qty": qty,
                                             "entry": trade.entry, "stop": trade.broker_sl,
                                             "target": tp, "exit_mode": sig.get("exit_mode")})
        return pos

    def update(self, frames_by_symbol: Mapping[str, Mapping[str, pd.DataFrame]]) -> List[Dict[str, Any]]:
        """Process every NEW closed LTF bar, oldest first, for each open position. Returns the exits made."""
        exits: List[Dict[str, Any]] = []
        for key, pos in list(self._open.items()):
            frames = frames_by_symbol.get(pos.symbol)
            if frames is None or pos.ltf_tf not in frames:
                continue
            ltf = closed(frames[pos.ltf_tf])
            new_times = [t for t in ltf.index if str(t) > pos.last_bar]
            for t in new_times:
                if key not in self._open:
                    break
                ex = self._process_bar(key, pos, frames, ltf, t)
                if ex is not None:
                    exits.append(ex)
            if key in self._open and new_times:
                pos.last_bar = str(new_times[-1])
        return exits

    def _process_bar(self, key: str, pos: Position, frames: Mapping[str, pd.DataFrame],
                     ltf: pd.DataFrame, t) -> Optional[Dict[str, Any]]:
        upto = ltf.loc[:t]
        bar = last_bars(upto, 1)[0]
        bar_time = str(t)
        if pos.strategy.exit_check(frames):
            return self._close(key, "HTF", bar.close, bar_time)
        hit = pos.trade.intrabar_exit(bar.high, bar.low)
        if hit == "DISASTER":
            price = min(bar.open, pos.trade.broker_sl)
        elif hit == "TP":
            price = max(bar.open, pos.trade.tp)
        else:
            highs = [float(x) for x in upto["high"]]
            lows = [float(x) for x in upto["low"]]
            closes = [float(x) for x in upto["close"]]
            atr_v = atr_series(highs, lows, closes, period=pos.atr_period)[-1]
            hit = pos.trade.on_closed_bar(high=bar.high, close=bar.close, atr=atr_v, p=pos.protection)
            price = bar.close
        if hit:
            return self._close(key, hit, price, bar_time)
        return None

    def _close(self, key: str, reason: str, price: float, bar_time: str) -> Dict[str, Any]:
        pos = self._open.pop(key)
        pos.strategy.on_exit(reason, float(price))
        pnl = (float(price) - pos.trade.entry) * pos.qty
        record = {"key": key, "symbol": pos.symbol, "reason": reason, "qty": pos.qty,
                  "entry": pos.trade.entry, "exit": float(price), "pnl": round(pnl, 4),
                  "exit_bar": bar_time, "mode": "PAPER"}
        self.closed_trades.append(record)
        self._audit.append("exit", record)
        return record
