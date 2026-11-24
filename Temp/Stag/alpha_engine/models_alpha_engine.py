"""Alpha engine wrapper: candidate signals from fundamentals and technicals, DELEGATED to Blue_engine.

Flow stage: Alpha engine
Status: PHASE 5 IMPLEMENTED (wrap; advice only, never an order)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/alpha/alpha_engine.py
(AlphaEngine)

Inputs arrive from the caller through config (``alpha_inputs``). The wrapper fetches
nothing. The output names candidates; it does not size or send anything.

FAIL-CLOSED RULES
-----------------
- Blue unavailable, or the engine raises: ``ok=False`` and no signals.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["run_alpha"]


def run_alpha(config: Optional[Dict[str, Any]] = None,
              inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return ``{ok, signals, selected, advice_only, warnings, reason}``. Never raises."""
    failed = {"ok": False, "signals": [], "selected": [], "advice_only": True, "warnings": []}
    try:
        cls = load("alpha.alpha_engine").AlphaEngine
    except BlueEngineUnavailable as exc:
        return {**failed, "reason": f"Blue alpha engine unavailable: {exc}"}
    cfg = dict(config or {})
    if inputs is not None:
        cfg["alpha_inputs"] = dict(inputs)
    try:
        result = cls(cfg).run({})
    except Exception as exc:  # noqa: BLE001
        return {**failed, "reason": f"alpha engine failed: {exc!r}"}
    data = getattr(result, "data", None) or {}
    return {
        "ok": bool(getattr(result, "ok", False)),
        "signals": list(data.get("signals") or []),
        "selected": list(data.get("selected") or []),
        "advice_only": True,
        "warnings": list(getattr(result, "warnings", []) or []),
        "reason": str(getattr(result, "error", None) or ""),
    }
