"""Market engine wrapper: market breadth, leaders, FII flow, DELEGATED to Blue_engine.

Flow stage: Market engine
Status: PHASE 5 IMPLEMENTED (wrap; advice only, no orders)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/market/market_engine.py
(MarketEngine)

WHAT THE OUTPUT IS ALLOWED TO DO
--------------------------------
Advice only. The output says how the market looks: breadth, leaders, laggards and
a risk-appetite score. It never sizes, sends or blocks an order. No quant lane
reads it for an order decision yet.

FAIL-CLOSED RULES
-----------------
- No market data: Blue returns a neutral "Elevated" state. The wrapper reports
  ``no_data=True``, ``risk_appetite=None`` and ``volatility_state="UNKNOWN"``, so
  a neutral default is never read as a real market signal.
- ``risk_appetite`` is clamped to [-1, 1]. A non-finite value becomes None.
- The wrapper never raises. A Blue load failure returns ``ok=False`` with an error.

The engine is PURE: market data arrives as a dict, no network, no SDK, no DB.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["MarketUnavailable", "RISK_MIN", "RISK_MAX", "run_market", "market_context"]

#: Bounds for the risk-appetite composite, as Blue defines it.
RISK_MIN = -1.0
RISK_MAX = 1.0


class MarketUnavailable(RuntimeError):
    """Blue's market engine cannot be loaded."""


def _engine_cls():
    try:
        module = load("market.market_engine")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise MarketUnavailable(f"Blue market engine unavailable: {exc}") from exc
    return module.MarketEngine


def run_market(config: Optional[Dict[str, Any]] = None,
               market: Optional[Dict[str, Any]] = None,
               provider: Optional[Callable[[], Dict[str, Any]]] = None,
               as_of: Any = None) -> Dict[str, Any]:
    """Run Blue's market engine on a market block. Never raises.

    ``market`` has the keys ``indices``, ``commodities`` and ``fii``. Priority is
    ``market`` > ``provider()`` > ``config["market"]``, which is Blue's own order.
    """
    try:
        cls = _engine_cls()
    except MarketUnavailable as exc:
        return {"ok": False, "engine": "market", "data": {}, "warnings": [],
                "error": str(exc)}
    context: Dict[str, Any] = {}
    if market is not None:
        context["market"] = dict(market)
    if as_of is not None:
        context["as_of"] = as_of
    try:
        engine = cls(dict(config or {}), provider=provider)
        result = engine.run(context)
    except Exception as exc:  # noqa: BLE001 - the wrapper must not raise
        return {"ok": False, "engine": "market", "data": {}, "warnings": [],
                "error": f"market engine failed: {exc!r}"}
    return {
        "ok": bool(getattr(result, "ok", False)),
        "engine": getattr(result, "engine", "market"),
        "data": dict(getattr(result, "data", {}) or {}),
        "warnings": list(getattr(result, "warnings", []) or []),
        "error": getattr(result, "error", None),
    }


def market_context(payload: Any) -> Dict[str, Any]:
    """Turn a Blue market payload into a bounded, fail-closed summary. Never raises.

    Returns ``{no_data, risk_appetite, volatility_state, leaders, laggards, reasons}``.
    """
    reasons: List[str] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"no_data": True, "risk_appetite": None, "volatility_state": "UNKNOWN",
                "leaders": [], "laggards": [], "reasons": ["no market payload"]}

    warnings = payload.get("warnings") or []
    no_data = any("no market data" in str(w).lower() for w in warnings)
    if no_data:
        reasons.append("no market data supplied: state unknown, not neutral")

    raw = data.get("risk_appetite")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    if not math.isfinite(value):
        risk = None
        reasons.append("risk appetite missing or non-finite: not used")
    else:
        risk = min(RISK_MAX, max(RISK_MIN, value))
        if risk != value:
            reasons.append(f"risk appetite {value:.4f} clamped to [{RISK_MIN}, {RISK_MAX}]")

    if no_data:
        risk = None
        volatility = "UNKNOWN"
    else:
        volatility = str(data.get("market_volatility_state") or "UNKNOWN")

    return {
        "no_data": no_data,
        "risk_appetite": None if risk is None else round(risk, 4),
        "volatility_state": volatility,
        "leaders": list(data.get("leaders") or [])[:5],
        "laggards": list(data.get("laggards") or [])[:5],
        "reasons": reasons,
    }
