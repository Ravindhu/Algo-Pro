"""Offline engine adapter for the existing EPV/CANAV valuation models."""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional

from ..core.base import BaseEngine, EngineResult, EngineStage
from .valuations import value_company


class ValuationEngine(BaseEngine):
    """Value supplied company snapshots without fetching or placing orders."""

    name = "valuation"
    stage = EngineStage.VALUATION

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 **kwargs: Any):
        super().__init__(config, provider, kwargs.get("log"))
        self.growth_rate_ceiling = self._bounded_config(
            "growth_rate_ceiling", 0.05, upper=1.0)
        self.margin_safety_adjustment = self._bounded_config(
            "margin_safety_adjustment", 0.85, upper=1.0)

    def _bounded_config(self, key: str, default: float, *,
                        upper: float) -> float:
        raw = self.config.get(key, default)
        if isinstance(raw, bool):
            raise ValueError(f"{key} must be numeric")
        value = float(raw)
        if not math.isfinite(value) or not 0.0 <= value <= upper:
            raise ValueError(f"{key} must be finite and between 0 and {upper}")
        return value

    def _inputs(self, context: Dict[str, Any]) -> Dict[str, Any]:
        supplied = context.get("valuation_inputs")
        if isinstance(supplied, dict):
            return supplied
        if callable(self.provider):
            fetched = self.provider()
            if not isinstance(fetched, dict):
                raise TypeError("valuation provider must return a dictionary")
            return fetched
        configured = self.config.get("valuation_inputs")
        return configured if isinstance(configured, dict) else {}

    def _run(self, context: Dict[str, Any]) -> EngineResult:
        inputs = self._inputs(context)
        records = inputs.get("companies", inputs)
        if not isinstance(records, dict) or not records:
            return EngineResult(
                engine=self.name,
                ok=True,
                data={
                    "target_ticker": "",
                    "growth_rate_ceiling": self.growth_rate_ceiling,
                    "margin_safety_adjustment": self.margin_safety_adjustment,
                    "rationale": "No company financial snapshots supplied; no valuation estimated.",
                    "valuations": {},
                },
                warnings=["valuation inputs missing"],
            )

        valuations: Dict[str, Dict[str, Any]] = {}
        for raw_symbol, company in records.items():
            symbol = str(raw_symbol).strip().upper()
            if not symbol or not isinstance(company, dict):
                raise ValueError("valuation companies must map non-empty tickers to objects")
            result = value_company(
                company,
                symbol=symbol,
                basis=str(inputs.get("basis", "consolidated")),
                config=self.config.get("model_config"),
            )
            valuations[symbol] = result.as_dict()

        requested = str(inputs.get("target_ticker") or "").strip().upper()
        target = requested if requested in valuations else next(iter(valuations))
        if requested and requested not in valuations:
            raise ValueError(f"target_ticker {requested!r} is not in valuation inputs")

        target_value = valuations[target]
        classification = target_value.get("classification", "insufficient_data")
        reason = target_value.get("classification_reason") or target_value.get("reason") or ""
        rationale = f"{classification}: {reason}".strip(": ")
        rationale = " ".join(rationale.split()[:15])

        return EngineResult(
            engine=self.name,
            ok=True,
            data={
                "target_ticker": target,
                "growth_rate_ceiling": self.growth_rate_ceiling,
                "margin_safety_adjustment": self.margin_safety_adjustment,
                "rationale": rationale,
                "valuations": valuations,
                "valuation_policy_source": "configured conservative bounds",
            },
        )


__all__ = ["ValuationEngine"]
