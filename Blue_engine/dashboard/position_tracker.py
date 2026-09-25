"""position_tracker - virtual ledger for the dashboard.

Stubs are loaded lazily by blue_engine.dashboard.__init__ so this
file can be edited in isolation.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TradeRecord:
    symbol: str
    exchange: str
    side: str  # "BUY" | "SELL"
    qty: int
    price: float
    ts: float
    state: str
    pov_pct: float = 0.0


@dataclass
class VirtualPosition:
    symbol: str
    exchange: str
    qty: int = 0
    avg_price: Optional[float] = None
    realized_pnl: float = 0.0


class PositionTracker:
    def __init__(self):
        self._positions: Dict[str, VirtualPosition] = {}
        self._trades: List[TradeRecord] = []

    def get(self, symbol: str, exchange: str) -> VirtualPosition:
        key = f"{symbol}|{exchange}"
        if key not in self._positions:
            self._positions[key] = VirtualPosition(symbol=symbol, exchange=exchange)
        return self._positions[key]

    def record_trade(self, trade: TradeRecord) -> None:
        self._trades.append(trade)

    def recent_trades(self, limit: int = 20) -> List[TradeRecord]:
        return self._trades[-limit:]

    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self._positions.values())