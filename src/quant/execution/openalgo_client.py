"""Broker adapters. The only module that talks to OpenAlgo, and the only one that names the order call.

Flow stage: OpenAlgo
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/client.py load_env_file (ported to quant/core/env.py; no Blue import).
Kept local on purpose: the injected-transport order path and PaperBroker. See docs/audits/PHASE0_REUSE_AUDIT.md step 6.

Modes (decision 0026, docs/ANALYZER_MODE_PLAN.md):
  "live"    Live order guard (decision 0017, docs/HARDENING_PLAN_2026-11-06.md):
    1. A LiveApproval object must exist. Only a person creates it, with the approver,
       the date, and the SHA-256 of risk_limits.yaml the approver reviewed.
    2. The approval must match the limits file on disk right now. Changing the limits
       cancels the approval.
  "analyze" OpenAlgo analyzer (sandbox). No real money, so no LiveApproval. Before every
    send the client asks OpenAlgo whether analyze mode is on. If it is not on (or the
    answer is unclear), the send is refused, because the order could reach the broker.
  Both modes:
    3. A GateDecision that is approved and has the same intent_id must be passed in.
       A string or a missing decision is refused.
  "paper" is refused here. Paper fills use PaperBroker, which never calls this client.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Optional

from quant.core.types import GateDecision, TradeIntent
from quant.execution.retry import retry_exit
from quant.monitor.throttle import TokenBucket, acquire

Transport = Callable[[str, Dict], Dict]
MODES = ("live", "analyze")


@dataclass(frozen=True)
class LiveApproval:
    """Human sign-off for live trading. Created by a person, never by code paths in the trade flow."""

    approver: str
    approved_on: date
    limits_sha256: str


def file_sha256(path: Path) -> str:
    """SHA-256 of a file as hex. Used to tie an approval to one exact limits file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def http_transport(url: str, payload: Dict, timeout: float = 10.0) -> Dict:
    """POST JSON to OpenAlgo using only the standard library. Returns the parsed body.

    An HTTP error body is returned as-is when it is JSON (OpenAlgo sends errors that way).
    Network errors are raised. The payload is never logged: it holds the API key.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"status": "error", "message": f"http {exc.code}"}


class PaperBroker:
    """Simulated fills at the reference price plus slippage. No network."""

    def __init__(self, slippage_bps: float = 5.0) -> None:
        self.slippage_bps = slippage_bps
        self._n = 0

    def place(self, intent: TradeIntent, decision: Optional[GateDecision] = None) -> Dict:
        self._n += 1
        slip = self.slippage_bps / 10_000
        px = intent.ref_price * (1 + slip if intent.side == "BUY" else 1 - slip)
        return {"order_id": f"PAPER-{self._n}", "status": "FILLED", "filled_qty": intent.qty, "avg_price": px}


class OpenAlgoClient:
    """Sends orders to OpenAlgo. Refuses unless every guard for its mode passes (see module doc)."""

    PLACE_PATH = "/api/v1/placeorder"
    CANCEL_PATH = "/api/v1/cancelorder"
    ANALYZER_PATH = "/api/v1/analyzer"
    ORDERSTATUS_PATH = "/api/v1/orderstatus"

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
        """Read OPENALGO_API_KEY. Loads the repo-root .env first (quant.core.env, no Blue import).

        Does NOT call Blue's resolve_api_key: that also decrypts keys from the OpenAlgo
        database. This path reads the environment only, so no stored secret is opened.
        """
        from quant.core.env import load_env_file
        load_env_file()
        key = os.environ.get("OPENALGO_API_KEY", "")
        if not key:
            raise RuntimeError("OPENALGO_API_KEY is not set")
        return key

    def _check_mode(self) -> None:
        if self.mode not in MODES:
            raise PermissionError("OpenAlgoClient refuses to send orders unless mode is live or analyze")

    def _check_live_session(self) -> None:
        """Live only: human approval and an unchanged limits file."""
        if not isinstance(self.live_approval, LiveApproval) or not self.live_approval.approver.strip():
            raise PermissionError("live trading needs a recorded human approval (LiveApproval)")
        if self.limits_path is None:
            raise PermissionError("live trading needs the limits file path to check the approval")
        if file_sha256(self.limits_path) != self.live_approval.limits_sha256:
            raise PermissionError("risk_limits.yaml changed since approval; approval is void")

    def _require_analyzer_on(self, api_key: str) -> None:
        """Analyze only: ask OpenAlgo to confirm the sandbox is on. Anything unclear is refused."""
        if not api_key:
            raise PermissionError("analyze refused: empty API key")
        resp = self._transport(self.base_url + self.ANALYZER_PATH, {"apikey": api_key})
        data = resp.get("data") if isinstance(resp, dict) else None
        on = (isinstance(resp, dict) and resp.get("status") == "success"
              and isinstance(data, dict) and data.get("analyze_mode") is True)
        if not on:
            raise PermissionError("analyze refused: OpenAlgo analyzer is not on; the order could reach the broker")

    def _check_session_guard(self, api_key: str = "") -> None:
        """Mode checks shared by every send. No intent needed."""
        self._check_mode()
        if self.mode == "live":
            self._check_live_session()
        else:
            self._require_analyzer_on(api_key)

    def _check_live_guard(self, intent: TradeIntent, decision: GateDecision, api_key: str = "") -> None:
        """Raise PermissionError on the first failed guard. Pure checks, no I/O except the limits hash and analyzer query."""
        self._check_session_guard(api_key)
        if not isinstance(decision, GateDecision) or not decision.approved:
            raise PermissionError("order refused: no approved gate decision")
        if decision.intent_id != intent.intent_id:
            raise PermissionError("order refused: gate decision does not match the intent")

    def place(self, intent: TradeIntent, api_key: str, decision: GateDecision) -> Dict:
        self._check_live_guard(intent, decision, api_key)
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

    def place_child(
        self,
        intent: TradeIntent,
        api_key: str,
        decision: GateDecision,
        price_type: str = "MARKET",
        limit_price: Optional[float] = None,
    ) -> Dict:
        """Send one child order from the POV engine (decision 0021). Same guard as place().

        price_type is MARKET or LIMIT. LIMIT needs a finite, positive limit_price.
        PEG is not supported yet: it needs its own design note.
        """
        if price_type not in ("MARKET", "LIMIT"):
            raise ValueError(f"unsupported price_type: {price_type!r}")
        if price_type == "LIMIT" and (limit_price is None or not math.isfinite(limit_price) or limit_price <= 0):
            raise ValueError("LIMIT child order needs a finite, positive limit_price")
        self._check_live_guard(intent, decision, api_key)
        if not api_key:
            raise PermissionError("order refused: empty API key")
        payload = {
            "apikey": api_key,
            "symbol": intent.symbol,
            "action": intent.side,
            "quantity": intent.qty,
            "pricetype": price_type,
            "product": "MIS",
            "strategy": intent.strategy_id,
            "tag": intent.intent_id,
        }
        if price_type == "LIMIT":
            payload["price"] = limit_price
        return self._transport(self.base_url + self.PLACE_PATH, payload)

    def cancel(self, order_id: str, strategy: str, api_key: str) -> Dict:
        """Cancel one child order (decision 0021).

        Cancels reduce risk, so there is no GateDecision here. The session guard still applies:
        live mode with a recorded approval and an unchanged limits file, or analyze mode with the
        sandbox confirmed on. Owner to confirm this choice.
        """
        self._check_session_guard(api_key)
        if not api_key:
            raise PermissionError("cancel refused: empty API key")
        if not order_id:
            raise ValueError("cancel needs an order_id")
        payload = {"apikey": api_key, "orderid": str(order_id), "strategy": strategy}
        return self._transport(self.base_url + self.CANCEL_PATH, payload)

    def get_order_status(self, api_key: str, order_id: str, strategy: str) -> Dict:
        """Read one order's status (a read, not an order). Used by AnalyzeBroker to confirm a fill."""
        if not api_key:
            raise ValueError("order status needs an API key")
        if not order_id:
            raise ValueError("order status needs an order_id")
        payload = {"apikey": api_key, "orderid": str(order_id), "strategy": strategy}
        return self._transport(self.base_url + self.ORDERSTATUS_PATH, payload)


class AnalyzeBroker:
    """Adapter for run_paper: sends through OpenAlgo analyze mode, then reads the order status.

    Result status is one of:
      FILLED    OpenAlgo says "complete" with a positive average price.
      REJECTED  The guard refused, OpenAlgo returned an error, or the order was rejected/cancelled.
      UNKNOWN   Network error, timeout, or an order still open. The order may exist: do not resend.
                An exit is retried only when the broker confirms no open order and the position is still open
                (execution/retry.py). Orders are limited to 8 per second.
    """

    ORDER_RATE_PER_SEC = 8  # 80% of OpenAlgo's documented 10 per second for placeorder

    def __init__(self, client: OpenAlgoClient, api_key: str, alerts=None,
                 order_not_placed=None, position_still_open=None) -> None:
        if not api_key:
            raise ValueError("AnalyzeBroker needs an API key")
        self._client = client
        self._api_key = api_key
        self._alerts = alerts
        self._order_not_placed = order_not_placed
        self._position_still_open = position_still_open
        self._bucket = TokenBucket(self.ORDER_RATE_PER_SEC, self.ORDER_RATE_PER_SEC)

    def attach_alerts(self, alerts) -> None:
        """Set where CRITICAL alerts go (run_paper sends them to the audit log)."""
        self._alerts = alerts

    def place(self, intent: TradeIntent, decision: GateDecision) -> Dict:
        if not acquire(self._bucket):
            return {"order_id": "", "status": "REJECTED", "filled_qty": 0, "avg_price": None,
                    "reason": "order-rate throttle: not sent (no token within 2 s)"}
        result = self._place_once(intent, decision)
        if intent.kind == "exit":
            result = retry_exit(
                lambda i, d: self._place_once(i, d), intent, decision,
                order_not_placed=self._order_not_placed,
                position_still_open=self._position_still_open,
                alerts=self._alerts,
            )
        return result

    def _place_once(self, intent: TradeIntent, decision: GateDecision) -> Dict:
        def out(status: str, order_id: str = "", reason: str = "", avg: Optional[float] = None, qty: int = 0) -> Dict:
            return {"order_id": order_id, "status": status, "filled_qty": qty, "avg_price": avg, "reason": reason}

        try:
            resp = self._client.place(intent, self._api_key, decision)
        except PermissionError as exc:
            return out("REJECTED", reason=str(exc))
        except Exception as exc:  # network or parse error: the order state is unknown
            return out("UNKNOWN", reason=repr(exc))

        order_id = str(resp.get("orderid", "")) if isinstance(resp, dict) else ""
        if not isinstance(resp, dict) or resp.get("status") != "success" or not order_id:
            return out("REJECTED", reason=str(resp.get("message", "no order id")) if isinstance(resp, dict) else "bad reply")

        try:
            st = self._client.get_order_status(self._api_key, order_id, intent.strategy_id)
        except Exception as exc:
            return out("UNKNOWN", order_id=order_id, reason=repr(exc))
        data = st.get("data") if isinstance(st, dict) and isinstance(st.get("data"), dict) else st
        if not isinstance(data, dict):
            return out("UNKNOWN", order_id=order_id, reason="status reply has no data")

        state = str(data.get("order_status", "")).lower()
        avg = data.get("average_price")
        if state == "complete" and isinstance(avg, (int, float)) and math.isfinite(avg) and avg > 0:
            return out("FILLED", order_id=order_id, avg=float(avg), qty=intent.qty)
        if state in ("rejected", "cancelled", "canceled"):
            return out("REJECTED", order_id=order_id, reason=f"order {state}")
        return out("UNKNOWN", order_id=order_id, reason=f"order state {state or 'missing'}")
