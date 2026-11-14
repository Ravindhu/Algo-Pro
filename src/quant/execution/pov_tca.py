"""POV execution cost (TCA) from a slicer summary (decouple 6.5, Step 1). Pure.

Flow stage: POV post-trade analysis
Status: PHASE 3 IMPLEMENTED (ported 2026-11-09 from src/quant/blue_engine/pov/pov_engine.py PovEngine._execution_cost)
Parity: tests/parity/test_pov_step1_parity.py

Metrics:
  fill_rate        filled / target
  child_orders     count of child orders
  avg_child_qty    mean attempted quantity per child
  price_drift_bps  (last child LTP - first child LTP) / first, in basis points
  adverse_drift_bps drift as adverse for the phase: sign flipped when the phase is "exiting"
No I/O. Input is the summary dict; a missing field gives None, not an error.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def execution_cost(summary: Mapping[str, Any]) -> Dict[str, Any]:
    filled = int(summary.get("filled") or 0)
    target = int(summary.get("target") or 0)
    children = summary.get("children") or []
    metrics: Dict[str, Any] = {
        "fill_rate": round(filled / target, 4) if target else None,
        "child_orders": int(summary.get("child_orders") or len(children)),
        "avg_child_qty": None,
        "price_drift_bps": None,
    }
    if children:
        qtys = [int(c.get("attempted") or 0) for c in children]
        if qtys:
            metrics["avg_child_qty"] = round(sum(qtys) / len(qtys), 2)
        ltps = [_safe_float(c.get("ltp_at_place")) for c in children]
        ltps = [p for p in ltps if p and p > 0]
        if len(ltps) >= 2:
            drift = (ltps[-1] - ltps[0]) / ltps[0] * 10_000.0
            phase = str(summary.get("phase") or "accumulating")
            metrics["price_drift_bps"] = round(drift, 2)
            metrics["adverse_drift_bps"] = round(drift if phase != "exiting" else -drift, 2)
    return metrics
