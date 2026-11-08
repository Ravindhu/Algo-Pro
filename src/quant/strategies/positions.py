"""Paper position manager: exits for investment trades with trailing stop and breakeven, as in Algo-Lab.

Flow stage: Exit manager (paper)
Status: PHASE 2 IMPLEMENTED (paper only, 2026-11-08; not live).

No simulated exit scenarios. Each open trade is managed by the same rules as Algo-Lab
(quant.strategies.protect.Trade), once per NEW closed LTF bar, oldest first:
  1. HTF floor: the strategy's exit_check() says the HTF closed below the floor      -> "HTF"
  2. Intrabar (this bar's high and low):
       low <= broker disaster stop                                                  -> "DISASTER"
       high >= fixed target (FIXED exits only)                                      -> "TP"
  3. Close rules: close below the stop (initial stop or breakeven stop)              -> "SL"
       close below the ATR trail                                                    -> "TRAIL"
Paper fill prices: DISASTER at min(open, stop); TP at max(open, target); SL, TRAIL, HTF at the bar close.

Every exit is an order intent. It goes through the same executor as entries, so it passes the Risk
gate and the approved queue. If the gate rejects it, the exit stays PENDING and is retried on every
cycle. The position is closed only when the exit is sent. A pending exit blocks new bar processing for
that position, so the stop is never skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Set

import pandas as pd

from quant.core.types import TradeIntent
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
    pending: Optional[Dict[str, Any]] = None   # exit waiting for the gate: reason, price, bar_time


class PaperPositionManager:
    def __init__(self, audit: Any, submit: Optional[Callable[[TradeIntent], Any]] = None) -> None:
        """submit(intent) -> ChildOutcome-like object with .sent and .reasons (the PovExecutor.submit_child).
        Without it, exits cannot be sent, so the manager raises rather than closing a position silently."""
        self._audit = audit
        self._submit = submit
        self._open: Dict[str, Position] = {}
        self._adv: Mapping[str, float] = {}
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

    def update(self, frames_by_symbol: Mapping[str, Mapping[str, pd.DataFrame]],
               adv_shares: Optional[Mapping[str, float]] = None) -> List[Dict[str, Any]]:
        """Send pending exits, then process new closed bars. Returns the exits sent in this call."""
        self._adv = dict(adv_shares or {})
        sent: List[Dict[str, Any]] = []
        for key, pos in list(self._open.items()):
            if pos.pending is not None:
                rec = self._send_exit(key, pos)
                if rec is not None:
                    sent.append(rec)
                continue                                    # no new bars while an exit is pending
            frames = frames_by_symbol.get(pos.symbol)
            if frames is None or pos.ltf_tf not in frames:
                continue
            ltf = closed(frames[pos.ltf_tf])
            new_times = [t for t in ltf.index if str(t) > pos.last_bar]
            for t in new_times:
                if key not in self._open or self._open[key].pending is not None:
                    break
                self._process_bar(key, pos, frames, ltf, t)
                if key in self._open and self._open[key].pending is None:
                    pos.last_bar = str(t)
                if key in self._open and self._open[key].pending is not None:
                    rec = self._send_exit(key, self._open[key])
                    if rec is not None:
                        sent.append(rec)
                    break
                if key not in self._open:
                    break
        return sent

    def _process_bar(self, key: str, pos: Position, frames: Mapping[str, pd.DataFrame],
                     ltf: pd.DataFrame, t) -> None:
        upto = ltf.loc[:t]
        bar = last_bars(upto, 1)[0]
        bar_time = str(t)
        if pos.strategy.exit_check(frames):
            pos.pending = {"reason": "HTF", "price": float(bar.close), "bar_time": bar_time}
            return
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
            pos.pending = {"reason": hit, "price": float(price), "bar_time": bar_time}

    def _send_exit(self, key: str, pos: Position) -> Optional[Dict[str, Any]]:
        if self._submit is None:
            raise RuntimeError("PaperPositionManager needs a submit function to send exits")
        p = pos.pending
        intent = TradeIntent(
            intent_id=f"{key}-EXIT-{p['bar_time']}", strategy_id=pos.strategy.name, symbol=pos.symbol,
            side="SELL", qty=pos.qty, ref_price=float(p["price"]), kind="exit", lot_size=1,
            adv_shares=self._adv.get(pos.symbol),
            trade_day=pd.Timestamp(p["bar_time"]).date())
        outcome = self._submit(intent)
        if not outcome.sent:
            self._audit.append("exit_pending", {"key": key, "reason": p["reason"],
                                                "reasons": list(outcome.reasons)})
            return None
        return self._close(key, p["reason"], p["price"], p["bar_time"])

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

    # ---- persistence (used by the state store) ----
    def to_state(self) -> Dict[str, Any]:
        out = {}
        for key, p in self._open.items():
            out[key] = {"strategy_id": f"{p.strategy.name}|{p.symbol}", "symbol": p.symbol,
                        "qty": p.qty, "trade": p.trade.to_dict(), "protection": p.protection.to_dict(),
                        "atr_period": p.atr_period, "ltf_tf": p.ltf_tf, "entry_time": p.entry_time,
                        "last_bar": p.last_bar, "pending": p.pending}
        return out

    def restore_state(self, data: Mapping[str, Any], strategies: Mapping[str, Any]) -> None:
        """Rebuild open positions. strategies maps 'name|symbol' to the live strategy object."""
        for key, d in data.items():
            sid = d["strategy_id"]
            if sid not in strategies:
                raise KeyError(f"saved position {key} belongs to unknown strategy {sid}")
            strat = strategies[sid]
            self._open[key] = Position(
                key=key, strategy=strat, symbol=d["symbol"], qty=int(d["qty"]),
                trade=Trade.from_dict(d["trade"]), protection=Protection.from_dict(d["protection"]),
                atr_period=int(d["atr_period"]), ltf_tf=d["ltf_tf"], entry_time=d["entry_time"],
                last_bar=d["last_bar"], pending=d.get("pending"))
