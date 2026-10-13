"""Deterministic, order-free risk decision stage for macro pipeline inputs."""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional

from .base import BaseEngine, EngineResult, EngineStage
from .risk_gate import RiskGate


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _data_block(context: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = context.get(key)
    if not isinstance(value, dict):
        return {}
    nested = value.get("data")
    return nested if isinstance(nested, dict) else value


class RiskEngine(BaseEngine):
    """Turn explicit incident/limit inputs into a fail-closed veto decision.

    The engine only returns recommendations. Actual order admission remains
    enforced by the runner's pre-trade ``RiskGate``.
    """

    name = "risk"
    stage = EngineStage.RISK

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 **kwargs: Any):
        super().__init__(config, provider, kwargs.get("log"))
        self.max_latency_ms = float(self.config.get("max_network_latency_ms", 5000))
        self.max_slippage_pct = float(self.config.get("max_allowed_slippage_pct", 0.005))
        self.trailing_stop_pct = float(self.config.get("force_trailing_stop_pct", 2.5))
        if not math.isfinite(self.max_latency_ms) or self.max_latency_ms <= 0:
            raise ValueError("max_network_latency_ms must be finite and positive")
        if not math.isfinite(self.max_slippage_pct) or self.max_slippage_pct < 0:
            raise ValueError("max_allowed_slippage_pct must be finite and non-negative")
        if not math.isfinite(self.trailing_stop_pct) or self.trailing_stop_pct <= 0:
            raise ValueError("force_trailing_stop_pct must be finite and positive")

    def _inputs(self, context: Dict[str, Any]) -> Dict[str, Any]:
        supplied = context.get("risk_inputs")
        if isinstance(supplied, dict):
            return supplied
        if callable(self.provider):
            fetched = self.provider()
            if not isinstance(fetched, dict):
                raise TypeError("risk provider must return a dictionary")
            return fetched
        configured = self.config.get("risk_inputs")
        if isinstance(configured, dict):
            return configured
        incident_keys = {
            "risk_veto_active", "latency_anomaly", "latency_ms",
            "corrupted_data", "conflicting_reports", "extreme_market_panic",
            "network_down", "observed_slippage_pct",
        }
        if incident_keys.intersection(self.config):
            return self.config
        return {}

    def _run(self, context: Dict[str, Any]) -> EngineResult:
        inputs = self._inputs(context)
        warnings = []
        breaches = []

        if not inputs:
            live = not bool(context.get("dry_run", False))
            if live:
                breaches.append("risk inputs unavailable in live mode")
            else:
                warnings.append("risk inputs unavailable; no incident assessment performed")

        if bool(inputs.get("risk_veto_active")):
            breaches.append(str(inputs.get("veto_reason") or "risk veto explicitly requested"))

        flags = (
            ("latency_anomaly", "network latency anomaly"),
            ("corrupted_data", "market data integrity failure"),
            ("conflicting_reports", "conflicting market reports"),
            ("extreme_market_panic", "extreme market tail-risk alert"),
            ("network_down", "network unavailable"),
        )
        for key, reason in flags:
            if bool(inputs.get(key)):
                breaches.append(reason)

        latency = inputs.get("latency_ms")
        if latency is not None:
            parsed_latency = _number(latency)
            if parsed_latency is None or parsed_latency < 0:
                breaches.append("invalid network latency measurement")
            elif parsed_latency > self.max_latency_ms:
                breaches.append(
                    f"network latency {parsed_latency:g}ms exceeds "
                    f"{self.max_latency_ms:g}ms limit")

        market = _data_block(context, "market")
        if str(market.get("market_volatility_state", "")).lower() == "high_panic":
            breaches.append("market volatility is in high-panic state")

        observed_slippage = inputs.get("observed_slippage_pct")
        if observed_slippage is not None:
            parsed_slippage = _number(observed_slippage)
            if parsed_slippage is None or parsed_slippage < 0:
                breaches.append("invalid observed slippage measurement")
            elif parsed_slippage > self.max_slippage_pct:
                breaches.append(
                    f"observed slippage {parsed_slippage:g} exceeds "
                    f"{self.max_slippage_pct:g} limit")

        intent = context.get("intent")
        gate_cfg = self.config.get("risk_gate")
        if isinstance(intent, dict) and isinstance(gate_cfg, dict) and gate_cfg:
            decision = RiskGate(gate_cfg).check(
                intent,
                day_pnl=_number(inputs.get("day_pnl")),
                open_exposure=_number(inputs.get("open_exposure")),
            )
            if not decision.approved:
                breaches.extend(decision.breaches)

        veto = bool(breaches)
        rationale = ("; ".join(breaches) if veto else
                     "No supplied risk trigger detected; this is not a connectivity health check.")
        rationale = " ".join(rationale.split()[:15])
        self.log(f"veto={veto} breaches={len(breaches)}")
        return EngineResult(
            engine=self.name,
            ok=True,
            data={
                "risk_veto_active": veto,
                "max_allowed_slippage_pct": self.max_slippage_pct,
                "force_trailing_stop_pct": self.trailing_stop_pct,
                "veto_reason": rationale,
                "breaches": breaches,
                "risk_assessed": bool(inputs),
            },
            warnings=warnings,
        )


__all__ = ["RiskEngine"]
