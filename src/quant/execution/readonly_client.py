"""Read-only OpenAlgo adapter: quotes, depth and orderbook. No order methods (decision 0021).

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
