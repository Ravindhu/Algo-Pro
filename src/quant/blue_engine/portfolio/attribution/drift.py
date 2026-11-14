"""
attribution/drift.py - target-vs-actual drift tracking (pure).

Between rebalances, real positions drift away from target simply because prices
move. This module measures that drift and turns it into the numbers the
Allocation tab charts:

* ``drift``               per-sleeve (actual - target) at one point in time.
* ``drift_series``        the drift of each sleeve over a history of snapshots.
* ``tracking_difference`` the L1/L2 distance between actual and target.
* ``time_since_target``   how stale the target is.

Snapshots are plain dicts: ``{"ts": float, "target": {...}, "actual": {...}}``
so they can be persisted by ``portfolio_state`` and replayed offline.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["drift", "tracking_difference", "drift_series", "breaches",
           "latest_drift"]


def drift(target: Dict[str, float],
          actual: Dict[str, float]) -> Dict[str, Any]:
    """Per-sleeve drift (actual - target) plus a summary.

    Returns ``{"sleeves": {k: {"target","actual","drift","drift_pct"}},
    "max_drift", "total_abs_drift", "cash_target", "cash_actual"}``.
    """
    keys = set(target) | set(actual)
    rows: Dict[str, Dict[str, Any]] = {}
    max_drift = 0.0
    total_abs = 0.0
    for k in keys:
        t = safe_float(target.get(k)) or 0.0
        a = safe_float(actual.get(k)) or 0.0
        d = a - t
        max_drift = max(max_drift, abs(d))
        total_abs += abs(d)
        rows[k] = {
            "target": round(t, 6), "actual": round(a, 6),
            "drift": round(d, 6),
            "drift_pct": round((d / t * 100.0) if t else None, 4)
            if t else None,
        }
    return {
        "sleeves": rows,
        "max_drift": round(max_drift, 6),
        "total_abs_drift": round(total_abs, 6),
        "cash_target": round(max(1.0 - sum(rows[k]["target"] for k in rows), 0.0), 6),
        "cash_actual": round(max(1.0 - sum(rows[k]["actual"] for k in rows), 0.0), 6),
    }


def tracking_difference(target: Dict[str, float], actual: Dict[str, float]
                        ) -> Dict[str, float]:
    """L1 and L2 distances between actual and target weights.

    ``l1`` is the total absolute drift (turnover needed to re-align, /2);
    ``l2`` is the Euclidean distance — a smoother measure better suited to
    charting than to cost.
    """
    keys = set(target) | set(actual)
    d = [( safe_float(actual.get(k)) or 0.0) - (safe_float(target.get(k)) or 0.0)
         for k in keys]
    return {
        "l1": round(sum(abs(x) for x in d) / 2.0, 6),
        "l2": round(sum(x * x for x in d) ** 0.5, 6),
    }


def drift_series(snapshots: Sequence[Dict[str, Any]],
                 sleeve: Optional[str] = None) -> Any:
    """Drift over a list of snapshots.

    With ``sleeve`` given, returns a list of ``(ts, drift)`` for that sleeve.
    Without, returns ``{sleeve: [(ts, drift), ...]}`` for all sleeves.
    """
    per: Dict[str, List[Any]] = {}
    for snap in snapshots or []:
        ts = safe_float(snap.get("ts")) or 0.0
        tgt = snap.get("target") or {}
        act = snap.get("actual") or {}
        for k in set(tgt) | set(act):
            val = (safe_float(act.get(k)) or 0.0) - (safe_float(tgt.get(k)) or 0.0)
            per.setdefault(k, []).append((ts, round(val, 6)))
    if sleeve is not None:
        return per.get(sleeve, [])
    return per


def breaches(target: Dict[str, float], actual: Dict[str, float], *,
             band: float = 0.02) -> Dict[str, Any]:
    """Which sleeves have drifted beyond a band (the rebalance trigger)."""
    keys = set(target) | set(actual)
    out: Dict[str, float] = {}
    for k in keys:
        d = (safe_float(actual.get(k)) or 0.0) - (safe_float(target.get(k)) or 0.0)
        if abs(d) >= band:
            out[k] = round(d, 6)
    return {"breaches": out, "count": len(out), "band": band}


def latest_drift(snapshots: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Drift of the most recent snapshot in a history."""
    if not snapshots:
        return None
    snap = snapshots[-1]
    out = drift(snap.get("target") or {}, snap.get("actual") or {})
    out["ts"] = snap.get("ts")
    return out