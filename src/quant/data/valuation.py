"""Valuation engine wrapper: conservative company valuations, DELEGATED to Blue_engine.

Flow stage: Valuation engine
Status: PHASE 5 IMPLEMENTED (wrap; offline; advice only, never an order)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/valuation_engine.py
(ValuationEngine)

Company snapshots arrive from the caller through config (``valuation_inputs``). The
wrapper fetches nothing. A missing snapshot gives no valuation, never a guessed one.

IMPORT ORDER
------------
Load ``screener`` before ``valuation_engine``. Blue's valuation module imports the
screener package, which imports the valuation module back. Loading it first raises
a circular ImportError (verified 2026-11-14).

FAIL-CLOSED RULES
-----------------
- Blue unavailable or the engine raises: ``ok=False``, no valuations.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["run_valuation"]


def run_valuation(config: Optional[Dict[str, Any]] = None,
                  inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return ``{ok, target_ticker, valuations, rationale, advice_only, reason}``."""
    failed = {"ok": False, "target_ticker": "", "valuations": {}, "rationale": "",
              "advice_only": True}
    try:
        # Blue quirk: importing valuation_engine first is a circular import. The
        # screener package must load first; it then imports valuation_engine cleanly.
        load("screener")
        cls = load("valuation_engine").ValuationEngine
    except BlueEngineUnavailable as exc:
        return {**failed, "reason": f"Blue valuation engine unavailable: {exc}"}
    cfg = dict(config or {})
    if inputs is not None:
        cfg["valuation_inputs"] = dict(inputs)
    try:
        result = cls(cfg).run({})
    except Exception as exc:  # noqa: BLE001
        return {**failed, "reason": f"valuation engine failed: {exc!r}"}
    data = getattr(result, "data", None) or {}
    return {
        "ok": bool(getattr(result, "ok", False)),
        "target_ticker": str(data.get("target_ticker") or ""),
        "valuations": dict(data.get("valuations") or {}),
        "rationale": str(data.get("rationale") or ""),
        "advice_only": True,
        "reason": str(getattr(result, "error", None) or ""),
    }
