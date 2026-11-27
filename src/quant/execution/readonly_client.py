"""Read-only OpenAlgo adapter: quotes, depth, orderbook, funds and positionbook. No order methods (decision 0021).

Flow stage: OpenAlgo
Status: PHASE 3 IMPLEMENTED (read-only; no order route in this module)

Why it is separate: the POV loop needs market reads and order-state reads, but reads must never
be able to place or cancel. This class has no place, cancel or modify method, and its transport
is only ever called with the read paths below. Tests check both rules.

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
    SEARCH_PATH = "/api/v1/search"
    SYMBOL_PATH = "/api/v1/symbol"
    MARGIN_PATH = "/api/v1/margin"
    ANALYZER_PATH = "/api/v1/analyzer"

    def __init__(self, base_url: str, transport: Transport) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    @staticmethod
    def _need_key(api_key: str) -> None:
        if not api_key:
            raise ValueError("read refused: empty API key")

    def analyzer_mode(self, api_key: str):
        """True when OpenAlgo's analyzer (sandbox) is on, False when off, None when unclear.

        Read-only. Used only to decide whether a sandbox-only band fallback may apply.
        """
        if not api_key:
            return None
        try:
            resp = self._transport(self.base_url + self.ANALYZER_PATH, {"apikey": api_key})
        except Exception:  # noqa: BLE001 - unclear is not a yes
            return None
        data = resp.get("data") if isinstance(resp, dict) else None
        if not (isinstance(resp, dict) and resp.get("status") == "success" and isinstance(data, dict)):
            return None
        value = data.get("analyze_mode")
        return value if isinstance(value, bool) else None

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

    def margin_for_order(self, api_key: str, symbol: str, exchange: str, action: str, quantity: int,
                     product: str, pricetype: str = "MARKET", price: float = 0.0) -> Dict:
        """Broker margin for one order (OpenAlgo /margin). Read only: the broker prices it, nothing is placed.

        The reply is checked against the order's full value before any MTF order (margin_entry.verify_leverage).
        """
        self._need_key(api_key)
        # Phase 2 (audit item 19): every other guard validates the direction; this one did not.
        action = str(action or "").strip().upper()
        if action not in ("BUY", "SELL"):
            raise ValueError(f"margin_for_order: action must be BUY or SELL, got {action!r}")
        position = {"symbol": symbol, "exchange": exchange, "action": action, "quantity": str(int(quantity)),
                    "product": product, "pricetype": pricetype, "price": str(price)}
        return self._transport(self.base_url + self.MARGIN_PATH, {"apikey": api_key, "positions": [position]})

    def symbol_info(self, api_key: str, symbol: str, exchange: str = "NSE") -> Dict:
        """Exact symbol record from OpenAlgo /symbol (includes lotsize). Read only; raw reply."""
        self._need_key(api_key)
        payload = {"apikey": api_key, "symbol": symbol, "exchange": exchange}
        return self._transport(self.base_url + self.SYMBOL_PATH, payload)

    def search_symbol(self, api_key: str, symbol: str, exchange: str = "NSE") -> Dict:
        """Instrument-master lookup (OpenAlgo /search). Read only; raw reply.

        Used for the broker lot size (SME lots differ from cash-equity lots). Returns the raw
        reply dict; the caller validates the rows.
        """
        self._need_key(api_key)
        payload = {"apikey": api_key, "symbol": symbol, "exchange": exchange}
        return self._transport(self.base_url + self.SEARCH_PATH, payload)
