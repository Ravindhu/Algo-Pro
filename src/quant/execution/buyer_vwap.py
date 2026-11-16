"""Buyer-initiated VWAP for each screened symbol, from live quote polls (2026-11-15).

Flow stage: Market inputs, Data and features
Status: PHASE 3 IMPLEMENTED (read only; no order route)

Method (the same rule the older slicer used on its tick stream):
- Poll the quote for each screened symbol. Read LTP, ask and cumulative day volume.
- The volume added since the last poll is the trade volume for that interval.
- That volume counts as buyer-initiated only when LTP >= ask at the poll.
- Buyer VWAP = sum(LTP x counted volume) / sum(counted volume), for the IST day.

Limits (stated, not hidden):
- Quote-poll based, not trade by trade. Volume between two polls is priced at that poll's LTP
  and classified at that poll's ask. Shorter poll gaps give a closer answer.
- A restart sets the baseline again on the first poll, so volume traded before it is not
  counted. The day's buyer VWAP is then partial.
- A quote with no LTP, ask or volume is not counted. Nothing is guessed.
- vwap() returns None until counted buyer volume exists for today. The bot then falls back to
  the fixed POV (strategy_bot._pov_for logs it).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Optional


@dataclass
class _SymState:
    day: str
    last_volume: Optional[float] = None
    buyer_notional: float = 0.0
    buyer_volume: float = 0.0
    counted_polls: int = 0
    polls: int = 0


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return None if x != x else x  # NaN is not a number


class BuyerVwapBook:
    """Keeps one buyer-VWAP accumulator per symbol for the current IST day."""

    def __init__(self, reader, api_key: str, exchange: str = "NSE") -> None:
        self._reader = reader
        self._key = api_key
        self._exchange = exchange
        self._day: str = ""
        self._state: Dict[str, _SymState] = {}

    def apply_quote(self, symbol: str, quote: Dict[str, Any], day: str) -> bool:
        """Apply one quote. Returns True when this interval's volume was counted as buyer-initiated."""
        if day != self._day:
            self._day = day
            self._state = {}  # new IST day: start every symbol again
        st = self._state.get(symbol)
        if st is None:
            st = self._state[symbol] = _SymState(day=day)
        st.polls += 1
        ltp = _num(quote.get("ltp"))
        ask = _num(quote.get("ask"))
        volume = _num(quote.get("volume"))
        if volume is None:
            return False
        prev = st.last_volume
        st.last_volume = volume
        if prev is None or volume <= prev:
            return False  # first poll sets the baseline; no growth or a reset adds nothing
        delta = volume - prev
        if ltp is None or ltp <= 0 or ask is None or ask <= 0:
            return False  # cannot classify this interval: not counted
        if ltp < ask:
            return False  # seller-initiated (or at bid side): not counted
        st.buyer_notional += ltp * delta
        st.buyer_volume += delta
        st.counted_polls += 1
        return True

    def poll(self, symbols: Iterable[str], now: datetime) -> Dict[str, str]:
        """Poll each symbol once. A failed poll is reported for that symbol and does not stop the rest."""
        day = now.date().isoformat()
        out: Dict[str, str] = {}
        for sym in symbols:
            try:
                reply = self._reader.quotes(self._key, sym, self._exchange)
            except Exception as exc:  # noqa: BLE001 - one bad quote must not stop the others
                out[sym] = f"error: {exc!r}"
                continue
            if (not isinstance(reply, dict) or str(reply.get("status", "")).lower() != "success"
                    or not isinstance(reply.get("data"), dict)):
                out[sym] = "no quote"
                continue
            out[sym] = "counted" if self.apply_quote(sym, reply["data"], day) else "not counted"
        return out

    def vwap(self, symbol: str) -> Optional[float]:
        """Buyer-initiated VWAP for today, or None when there is no counted buyer volume."""
        st = self._state.get(symbol)
        if st is None or st.day != self._day or st.buyer_volume <= 0:
            return None
        return st.buyer_notional / st.buyer_volume

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Audit view: per symbol, the counted buyer volume and VWAP for today."""
        return {
            sym: {"buyer_volume": st.buyer_volume, "buyer_vwap": (st.buyer_notional / st.buyer_volume)
                  if st.buyer_volume > 0 else None, "counted_polls": st.counted_polls, "polls": st.polls}
            for sym, st in self._state.items()
        }


__all__ = ["BuyerVwapBook"]
