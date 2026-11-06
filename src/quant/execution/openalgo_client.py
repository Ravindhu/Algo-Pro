"""Broker adapters. The only module that talks to OpenAlgo, and the only one that names the order call.

Flow stage: OpenAlgo
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/client.py (.env loader wrapped).
Kept local on purpose: the injected-transport order path and PaperBroker. See docs/audits/PHASE0_REUSE_AUDIT.md step 6.

Live order guard (decision 0017, docs/HARDENING_PLAN_2026-11-06.md):
  1. mode must be "live".
  2. A LiveApproval object must exist. Only a person creates it, with the approver,
     the date, and the SHA-256 of risk_limits.yaml the approver reviewed.
  3. The approval must match the limits file on disk right now. Changing the limits
     cancels the approval.
  4. A GateDecision that is approved and has the same intent_id must be passed in.
     A string or a missing decision is refused.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Optional

from quant.core.types import GateDecision, TradeIntent

Transport = Callable[[str, Dict], Dict]


@dataclass(frozen=True)
class LiveApproval:
    """Human sign-off for live trading. Created by a person, never by code paths in the trade flow."""

    approver: str
    approved_on: date
    limits_sha256: str


def file_sha256(path: Path) -> str:
    """SHA-256 of a file as hex. Used to tie an approval to one exact limits file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
    """Sends orders to OpenAlgo. Refuses unless every live guard passes (see module doc)."""

    PLACE_PATH = "/api/v1/placeorder"

    def __init__(
        self,
        base_url: str,
        transport: Transport,
        mode: str,
        live_approval: Optional[LiveApproval] = None,
        limits_path: Optional[Path] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self.mode = mode
        self.live_approval = live_approval
        self.limits_path = Path(limits_path) if limits_path is not None else None

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

    def _check_live_guard(self, intent: TradeIntent, decision: GateDecision) -> None:
        """Raise PermissionError on the first failed guard. Pure checks, no I/O except the limits hash."""
        if self.mode != "live":
            raise PermissionError("OpenAlgoClient refuses to send orders unless mode is live")
        if not isinstance(self.live_approval, LiveApproval) or not self.live_approval.approver.strip():
            raise PermissionError("live trading needs a recorded human approval (LiveApproval)")
        if self.limits_path is None:
            raise PermissionError("live trading needs the limits file path to check the approval")
        if file_sha256(self.limits_path) != self.live_approval.limits_sha256:
            raise PermissionError("risk_limits.yaml changed since approval; approval is void")
        if not isinstance(decision, GateDecision) or not decision.approved:
            raise PermissionError("order refused: no approved gate decision")
        if decision.intent_id != intent.intent_id:
            raise PermissionError("order refused: gate decision does not match the intent")

    def place(self, intent: TradeIntent, api_key: str, decision: GateDecision) -> Dict:
        self._check_live_guard(intent, decision)
        if not api_key:
            raise PermissionError("order refused: empty API key")
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
