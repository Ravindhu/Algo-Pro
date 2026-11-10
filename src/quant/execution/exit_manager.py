"""Exit rules for open positions: stop-loss, take-profit, breakeven and ATR trail. Pure logic, no I/O.

Flow stage: Protection, Session guards (exit side), Algo router
Status: PHASE 3 IMPLEMENTED (pure rules; the bot sends the exit through the risk gate)

Rules, checked once per closed bar (the bot runs every 5 minutes on 5-minute bars):
  1. Stop first. BUY: if the bar low is at or below the stop, exit. SELL: mirror.
     A gap through the stop fills at the bar open (the worse of open and stop).
  2. Target. BUY: if the bar high reaches the target, exit at the target. SELL: mirror.
  3. If a bar touches both stop and target, the stop wins. This is the conservative choice,
     because the order inside one bar is unknown.
  4. Breakeven. Once price has moved breakeven_r * R in favour (R = entry-to-stop distance),
     the stop moves to entry. It never moves back.
  5. ATR trail. After breakeven, the stop trails the best price seen by trail_mult * ATR.
     The stop only tightens.

The ATR is the entry-time ATR from the signal (not re-computed per bar). DV-ATR is not
implemented: no definition is in the source code yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass
class ExitConfig:
    breakeven_r: float = 1.0
    trail_mult: float = 2.0

    def __post_init__(self) -> None:
        if self.breakeven_r <= 0 or self.trail_mult <= 0:
            raise ValueError("breakeven_r and trail_mult must be > 0")


@dataclass
class Position:
    key: str
    symbol: str
    side: str                      # the entry side: BUY (long) or SELL (short)
    qty: int
    entry: float
    stop: float
    target: float
    atr: float
    be_done: bool = False
    best: float = 0.0             # best price seen since entry (high for BUY, low for SELL)
    exit_halted: bool = False      # set when an exit reply is unclear: no automatic resend
    adv_shares: Optional[float] = None
    prev_close: Optional[float] = None
    circuit_pct: Optional[float] = None
    strategy: str = ""
    risk_r: float = field(init=False)

    def __post_init__(self) -> None:
        self.side = self.side.upper()
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"bad side {self.side!r}")
        if self.qty <= 0:
            raise ValueError("position qty must be > 0")
        self.risk_r = abs(self.entry - self.stop)
        if self.risk_r <= 0:
            raise ValueError("stop must differ from entry")
        self.best = self.entry

    @property
    def exit_side(self) -> str:
        return "SELL" if self.side == "BUY" else "BUY"


def evaluate_bar(pos: Position, bar_open: float, bar_high: float, bar_low: float,
                 cfg: ExitConfig) -> Tuple[Optional[float], str]:
    """Check one closed bar. Updates pos.stop, pos.be_done and pos.best. Returns (exit_price, reason).

    exit_price is None when the position stays open."""
    if pos.side == "BUY":
        if bar_low <= pos.stop:
            return min(pos.stop, bar_open), "stop"
        if bar_high >= pos.target:
            return pos.target, "target"
        if not pos.be_done and bar_high >= pos.entry + cfg.breakeven_r * pos.risk_r:
            pos.stop = max(pos.stop, pos.entry)
            pos.be_done = True
        pos.best = max(pos.best, bar_high)
        if pos.be_done:
            pos.stop = max(pos.stop, pos.best - cfg.trail_mult * pos.atr)
        return None, ""

    # SELL: mirror of BUY
    if bar_high >= pos.stop:
        return max(pos.stop, bar_open), "stop"
    if bar_low <= pos.target:
        return pos.target, "target"
    if not pos.be_done and bar_low <= pos.entry - cfg.breakeven_r * pos.risk_r:
        pos.stop = min(pos.stop, pos.entry)
        pos.be_done = True
    pos.best = min(pos.best, bar_low)
    if pos.be_done:
        pos.stop = min(pos.stop, pos.best + cfg.trail_mult * pos.atr)
    return None, ""
