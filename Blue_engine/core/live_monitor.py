"""Fail-closed adapter between live broker state and the backtest monitor."""
from __future__ import annotations

import importlib
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Dict, Optional

from blue_engine.backtest.loops import LiveMonitor, ReoptimizationSignal
from blue_engine.core import kill_switch_store
from blue_engine.portfolio import optim_policy

logger = logging.getLogger(__name__)

__all__ = ["LiveMonitorGuard", "resolve_monitor_provider"]

_QUANTITY_KEYS = ("netqty", "net_qty", "net_quantity", "quantity", "qty")
_PRICE_KEYS = ("ltp", "last_price", "lastprice", "avgprc", "avg_price",
               "average_price", "buyavgprice")


def resolve_monitor_provider(reference: str) -> Callable[..., Dict[str, Any]]:
    """Resolve a trusted ``module:function`` telemetry provider."""
    if not isinstance(reference, str) or ":" not in reference:
        raise ValueError("monitor provider must use the 'module:function' format")
    module_name, function_name = reference.split(":", 1)
    if not module_name.strip() or not function_name.strip():
        raise ValueError("monitor provider must use the 'module:function' format")
    try:
        provider = getattr(importlib.import_module(module_name), function_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"could not load live monitor provider {reference!r}: "
                         f"{exc}") from exc
    if not callable(provider):
        raise ValueError(f"live monitor provider {reference!r} is not callable")
    return provider


class LiveMonitorGuard:
    """Authorizes each POV child order using verified broker and P&L inputs.

    The provider must return real live telemetry with ``live_equity``,
    ``live_trade_pnls``, ``live_daily_returns`` and
    ``historical_daily_returns`` sequences. Any unavailable or malformed
    safety input latches the shared durable kill switch and blocks orders.
    A monitor threshold trip pauses entries but permits verified exit-phase
    orders so the slicer can reduce existing exposure.
    """

    def __init__(self, *, client: Any, monitor: LiveMonitor,
                 provider: Callable[..., Dict[str, Any]],
                 monitor_config: Mapping[str, Any]) -> None:
        if not callable(provider):
            raise TypeError("live monitor provider must be callable")
        if not isinstance(monitor_config, Mapping):
            raise TypeError("monitor_config must be a mapping")
        if not kill_switch_store.store_available():
            raise RuntimeError(
                "durable kill-switch storage is unavailable; refusing to "
                "enable live monitoring")
        try:
            check_interval = float(
                monitor_config.get("check_interval_seconds", 30.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "check_interval_seconds must be finite and positive") from exc
        if not math.isfinite(check_interval) or check_interval <= 0:
            raise ValueError("check_interval_seconds must be finite and positive")
        for key in ("max_dd", "max_losing_streak"):
            value = monitor.envelope.get(key)
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"historical_envelope.{key} must be a finite non-negative number")
            if not math.isfinite(number) or number < 0:
                raise ValueError(
                    f"historical_envelope.{key} must be a finite non-negative number")
        self.client = client
        self.monitor = monitor
        self.provider = provider
        self.monitor_config = dict(monitor_config)
        self.check_interval_seconds = check_interval
        self._last_observed_at = 0.0
        self._halt_kind: Optional[str] = None
        self._halt_reason: Optional[str] = None
        self.monitor.subscribe(self._on_signal)

    @property
    def halted(self) -> bool:
        return self._halt_kind is not None

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    def _on_signal(self, signal: ReoptimizationSignal) -> None:
        if signal.triggered:
            reason = "; ".join(signal.reasons)
            self._latch("monitor_trigger", reason)

    def _latch(self, kind: str, reason: str) -> None:
        if self._halt_kind is None:
            self._halt_kind = kind
            self._halt_reason = reason
            optim_policy.trip_kill_switch(
                f"live monitor {kind}: {reason}")
            if not kill_switch_store.store_available():
                logger.critical(
                    "live safety trip may not survive restart because "
                    "durable kill-switch storage is unavailable")
            logger.critical("live order safety halt (%s): %s", kind, reason)

    def on_data_failure(self, reason: str) -> None:
        """Latch the shared safety halt when the quote feed reaches PAUSED."""
        self._latch("market_data_failure", reason)

    @staticmethod
    def _verify_positionbook(response: Any) -> Dict[str, Any]:
        if not isinstance(response, dict) or response.get("status") != "success":
            raise ValueError("broker positionbook did not return success")
        data = response.get("data")
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = next((data[key] for key in ("positions", "book", "rows")
                         if isinstance(data.get(key), list)), None)
        else:
            rows = None
        if not isinstance(rows, list):
            raise ValueError("broker positionbook has no verifiable position list")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"positionbook row {index} is malformed")
            normalized = {str(key).lower(): value for key, value in row.items()}
            quantity = LiveMonitorGuard._first_finite(
                normalized, _QUANTITY_KEYS)
            if quantity is None:
                raise ValueError(
                    f"positionbook row {index} has no valid net quantity")
            if quantity != 0:
                price = LiveMonitorGuard._first_finite(normalized, _PRICE_KEYS)
                if price is None or price <= 0:
                    raise ValueError(
                        f"open position row {index} has no valid reference price")
        return {"status": "success", "data": rows}

    @staticmethod
    def _first_finite(row: Mapping[str, Any],
                      keys: Sequence[str]) -> Optional[float]:
        for key in keys:
            value = row.get(key)
            if value in (None, ""):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                return number
        return None

    def _read_telemetry(self, positionbook: Dict[str, Any]
                        ) -> Dict[str, Any]:
        telemetry = self.provider(
            client=self.client,
            positionbook=positionbook,
            monitor_config=self.monitor_config)
        if not isinstance(telemetry, dict):
            raise ValueError("live monitor provider must return a dictionary")
        required = ("live_equity", "live_trade_pnls",
                    "live_daily_returns", "historical_daily_returns")
        for key in required:
            values = telemetry.get(key)
            if (not isinstance(values, Sequence)
                    or isinstance(values, (str, bytes))):
                raise ValueError(
                    f"live monitor telemetry is missing sequence {key!r}")
        if not telemetry["live_equity"]:
            raise ValueError("live monitor telemetry has no equity observations")
        if len(telemetry["live_daily_returns"]) < self.monitor.regime_window:
            raise ValueError("live daily-return history is too short")
        if (len(telemetry["historical_daily_returns"])
                < self.monitor.regime_window + 1):
            raise ValueError(
                "historical daily-return baseline is too short for regime checks")
        return {key: telemetry[key] for key in required}

    def _sample(self) -> bool:
        try:
            positionbook = self._verify_positionbook(
                self.client.positionbook())
            telemetry = self._read_telemetry(positionbook)
            self.monitor.evaluate(**telemetry)
            return True
        except Exception as exc:
            self._latch("verification_failure", repr(exc))
            logger.exception("live monitor could not verify account safety")
            return False

    def observe(self) -> bool:
        """Sample live safety inputs at a bounded cadence during quote polls."""
        switch = optim_policy.kill_switch_state()
        if switch["tripped"] and self._halt_kind is None:
            self._halt_kind = "persisted_kill_switch"
            self._halt_reason = str(switch.get("reason") or "persisted trip")
        if self._halt_kind not in (None, "monitor_trigger"):
            return False
        now = time.monotonic()
        if now - self._last_observed_at < self.check_interval_seconds:
            return True
        self._last_observed_at = now
        return self._sample()

    def authorize_order(self, *, phase: str, quantity: int,
                        action: str) -> Optional[int]:
        """Return the child quantity, zero to pause entry, None to abort.

        ``None`` denotes an unverifiable safety state and aborts this POV run.
        A triggered monitor returns zero for entries, while verified exits
        remain available.
        """
        if phase not in ("accumulating", "exiting"):
            self._latch("verification_failure",
                        f"unknown POV phase {phase!r}")
            return None
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            self._latch("verification_failure",
                        f"invalid child quantity {quantity!r}")
            return None
        if not isinstance(action, str) or action.upper() not in ("BUY", "SELL"):
            self._latch("verification_failure",
                        f"invalid child action {action!r}")
            return None

        switch = optim_policy.kill_switch_state()
        if switch["tripped"] and self._halt_kind is None:
            self._halt_kind = "persisted_kill_switch"
            self._halt_reason = str(switch.get("reason") or "persisted trip")
        if self._halt_kind not in (None, "monitor_trigger"):
            logger.error("live child order blocked by %s: %s",
                         self._halt_kind, self._halt_reason)
            return None

        if not self._sample():
            return None

        if self._halt_kind == "monitor_trigger":
            if phase != "exiting":
                logger.warning("live entry paused by monitor; action=%s",
                               action.upper())
                return 0
        return quantity
