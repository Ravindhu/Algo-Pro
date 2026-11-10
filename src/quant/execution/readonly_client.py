"""Read-only OpenAlgo adapter: quotes, depth, orderbook, funds and positionbook. No order methods (decision 0021).

Flow stage: OpenAlgo
Status: PHASE 3 IMPLEMENTED (read-only; no order route in this module)

Why it is separate: the POV loop needs market reads and order-state reads, but reads must never
be able to place or cancel. This class has no place, cancel or modify method, and its transport
is only ever called with the three read paths below. Tests check both rules.

No live guard here: a read sends no order and changes no broker state.
"""
from __future__ import annotations

from typing import Callable, Dict

Transport = Callable[[str, Dict], Dict]


class OpenAlgoReadOnly:
    QUOTES_PATH = "/api/v1/quotes"
    DEPTH_PATH = "/api/v1/depth"
    ORDERBOOK_PATH = "/api/v1/orderbook"
    FUNDS_PATH = "/api/v1/funds"
    POSITIONBOOK_PATH = "/api/v1/positionbook"
    HISTORY_PATH = "/api/v1/history"

    def __init__(self, base_url: str, transport: Transport) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    @staticmethod
    def _need_key(api_key: str) -> None:
        if not api_key:
            raise ValueError("read refused: empty API key")

    def quotes(self, api_key: str, symbol: str, exchange: str) -> Dict:
        self._need_key(api_key)
        payload = {"apikey": api_key, "symbol": symbol, "exchange": exchange}
        return self._transport(self.base_url + self.QUOTES_PATH, payload)

    def depth(self, api_key: str, symbol: str, exchange: str) -> Dict:
        self._need_key(api_key)
        payload = {"apikey": api_key, "symbol": symbol, "exchange": exchange}
        return self._transport(self.base_url + self.DEPTH_PATH, payload)

    def orderbook(self, api_key: str) -> Dict:
        self._need_key(api_key)
        return self._transport(self.base_url + self.ORDERBOOK_PATH, {"apikey": api_key})

    def funds(self, api_key: str) -> Dict:
        self._need_key(api_key)
        return self._transport(self.base_url + self.FUNDS_PATH, {"apikey": api_key})

    def positionbook(self, api_key: str) -> Dict:
        self._need_key(api_key)
        return self._transport(self.base_url + self.POSITIONBOOK_PATH, {"apikey": api_key})

    def history(self, api_key: str, symbol: str, exchange: str, interval: str,
                start_date: str, end_date: str) -> Dict:
        """Bars for one window (OpenAlgo /history). Read only; returns the raw reply."""
        self._need_key(api_key)
        payload = {"apikey": api_key, "symbol": symbol, "exchange": exchange, "interval": interval,
                   "start_date": start_date, "end_date": end_date, "source": "api"}
        return self._transport(self.base_url + self.HISTORY_PATH, payload)
