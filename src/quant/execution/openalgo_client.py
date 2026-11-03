"""Broker adapters. The only module that talks to OpenAlgo, and the only one that names the order call.

Flow stage: OpenAlgo
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/client.py (.env loader wrapped).
Kept local on purpose: the injected-transport order path and PaperBroker. See docs/PHASE0_REUSE_AUDIT.md step 6.
"""
from __future__ import annotations

import os
from typing import Callable, Dict, Optional

from quant.core.types import TradeIntent

Transport = Callable[[str, Dict], Dict]


class PaperBroker:
    """Simulated fills at the reference price plus slippage. No network."""

    def __init__(self, slippage_bps: float = 5.0) -> None:
        self.slippage_bps = slippage_bps
        self._n = 0

    def place(self, intent: TradeIntent) -> Dict:
        self._n += 1
        slip = self.slippage_bps / 10_000
        px = intent.ref_price * (1 + slip if intent.side == "BUY" else 1 - slip)
        return {"order_id": f"PAPER-{self._n}", "status": "FILLED", "filled_qty": intent.qty, "avg_price": px}


class OpenAlgoClient:
    """Sends orders to OpenAlgo. Refuses unless mode is live and a live approval is recorded."""

    PLACE_PATH = "/api/v1/placeorder"

    def __init__(self, base_url: str, transport: Transport, mode: str, live_approval: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self.mode = mode
        self.live_approval = live_approval

    @staticmethod
    def api_key_from_env() -> str:
        """Read OPENALGO_API_KEY. Loads the repo-root .env first, using Blue_engine's loader.

        Does NOT call Blue's resolve_api_key: that also decrypts keys from the OpenAlgo
        database. This path reads the environment only, so no stored secret is opened.
        """
        from quant.core.blue_bridge import load
        load("core.client").load_env_file()
        key = os.environ.get("OPENALGO_API_KEY", "")
        if not key:
            raise RuntimeError("OPENALGO_API_KEY is not set")
        return key

    def place(self, intent: TradeIntent, api_key: str) -> Dict:
        if self.mode != "live":
            raise PermissionError("OpenAlgoClient refuses to send orders unless mode is live")
        if not self.live_approval:
            raise PermissionError("live trading needs a recorded human approval")
        payload = {
            "apikey": api_key,
            "symbol": intent.symbol,
            "action": intent.side,
            "quantity": intent.qty,
            "pricetype": "MARKET",
            "product": "MIS",
            "strategy": intent.strategy_id,
            "tag": intent.intent_id,
        }
        return self._transport(self.base_url + self.PLACE_PATH, payload)
