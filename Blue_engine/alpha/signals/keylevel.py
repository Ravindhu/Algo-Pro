"""
keylevel.py - manual price-level signal strategy.

You set a trigger price (and direction). The signal polls the live LTP
via the OpenAlgo SDK and emits a trade intent ONLY when the price
crosses the trigger. After firing once, it stays quiet (one-shot per
run, just like ManualSignal).

Why "Keylevel"
--------------
"Keylevel" is a manual signal: the operator picks a specific price
(support, resistance, breakout level, etc.) and lets the strategy
slicer (POV) take over from there. Unlike ManualSignal which fires
unconditionally, Keylevel waits for the market to come to the level.

JSON configuration
------------------
Required:
    trigger_price:     float  the price to watch
    trigger_direction: "ABOVE" | "BELOW"  cross direction that fires

Optional:
    symbol:            string (defaults to cfg["symbol"])
    exchange:          string (defaults to cfg["exchange"])
    poll_seconds:      int    polling interval (default 5)
    quantity:          int    target qty (default cfg["total_quantity"])
    action:            string "BUY" or "SELL" (default cfg["action"])
    one_shot:          bool   fire only once (default True)
    tolerance:         float  cross tolerance (default 0.0)
    wait_seconds:      int    optional cap on how long to wait
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from blue_engine.core.time_utils import ist_now

from .base import BaseSignal


class KeylevelSignal(BaseSignal):
    """Manual price-level trigger.

    Watches the live LTP via client.quotes(symbol=..., exchange=...) and
    emits the trade intent the first time the price crosses the
    configured trigger level in the configured direction.
    """

    def __init__(self, client: Any, config: Dict[str, Any]):
        super().__init__(client, config)
        # Required config
        self.trigger_price: float = float(config["trigger_price"])
        direction = str(config.get("trigger_direction", "ABOVE")).upper().strip()
        if direction not in ("ABOVE", "BELOW"):
            raise ValueError(
                f"trigger_direction must be 'ABOVE' or 'BELOW', got {direction!r}"
            )
        self.trigger_direction: str = direction

        # Optional config with sensible defaults
        self.symbol: str = config.get("symbol", config["symbol"])
        self.exchange: str = config.get("exchange", config["exchange"])
        self.poll_seconds: int = int(config.get("poll_seconds", 5))
        self.tolerance: float = float(config.get("tolerance", 0.0))
        self.one_shot: bool = bool(config.get("one_shot", True))
        # Prefer explicit 'quantity' from the master JSON, fall back to
        # 'total_quantity' (legacy field name), then 0.
        qty = config.get("quantity", config.get("total_quantity", 0))
        self.quantity: int = int(qty)
        self.action: str = str(config.get("action", "BUY")).upper()
        # Optional upper bound on how long to wait for the trigger
        self.wait_seconds: Optional[int] = config.get("wait_seconds")

        # Internal state
        self._fired = False
        self._start_ts = time.time()

    def detect(self) -> Optional[Dict[str, Any]]:
        """Poll the LTP and emit a trade intent when the trigger is crossed.

        Returns:
            dict with action, quantity, tag, reason when fired.
            None while waiting.
        """
        if self.one_shot and self._fired:
            return None

        if self.wait_seconds is not None:
            elapsed = time.time() - self._start_ts
            if elapsed > float(self.wait_seconds):
                self.log(
                    f"wait_seconds={self.wait_seconds} elapsed without "
                    f"trigger; giving up"
                )
                self._fired = True
                return None

        quote = self.client.quotes(symbol=self.symbol, exchange=self.exchange)
        if quote.get("status") != "success":
            self.log(
                f"quote failed: {quote.get('message')}; "
                f"retrying in {self.poll_seconds}s"
            )
            time.sleep(self.poll_seconds)
            return None

        qd = quote.get("data") or {}
        try:
            ltp = float(qd.get("ltp", 0))
        except (TypeError, ValueError):
            ltp = 0.0
        if ltp <= 0:
            self.log(f"non-positive LTP ({ltp}); retrying")
            time.sleep(self.poll_seconds)
            return None

        crossed = self._crossed(ltp)
        if not crossed:
            self.log(
                f"waiting: ltp={ltp:.2f} trigger={self.trigger_price:.2f} "
                f"dir={self.trigger_direction} tol={self.tolerance:.2f}"
            )
            time.sleep(self.poll_seconds)
            return None

        # Trigger fired - emit the intent
        self._fired = True
        intent = {
            "action": self.action,
            "quantity": self.quantity,
            "tag": self.config.get("strategy_label", "Keylevel"),
            "reason": (
                f"keylevel {self.trigger_direction} {self.trigger_price:.2f} "
                f"hit at ltp={ltp:.2f} ({ist_now().strftime('%H:%M:%S')} IST)"
            ),
        }
        self.log(
            f"FIRED: {intent['action']} {intent['quantity']} -> {intent['reason']}"
        )
        return intent

    def _crossed(self, ltp: float) -> bool:
        """Return True if ltp has crossed the trigger in the configured dir."""
        if self.trigger_direction == "ABOVE":
            return ltp >= (self.trigger_price - self.tolerance)
        # BELOW
        return ltp <= (self.trigger_price + self.tolerance)

    def describe(self) -> str:
        """One-line description for logs."""
        return (
            f"{self.__class__.__name__} watching {self.symbol}/{self.exchange} "
            f"{self.trigger_direction} {self.trigger_price:.2f}"
        )

