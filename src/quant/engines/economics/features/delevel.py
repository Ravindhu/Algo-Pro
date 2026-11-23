"""
features/delevel.py - z-score of change vs a rolling baseline (de-levelling).

Macro levels are non-stationary (they drift). Comparing today's level to a
fixed scale (the baseline engine's ``tanh(value / scale)``) mixes a signal's
*trend* with its *surprise*. This module removes the level and standardises the
**change against a rolling window**, which is closer to what a desk trades.

    delta      = value - previous                     # period change
    z_delta    = (delta - mean(deltas)) / sd(deltas)   # over a rolling window

When only a level history is available (no precomputed deltas), we form deltas
from the series itself. Pure and dependency-free.

Note
----
The engine combines *both* views: ``surprise`` (vs expectation) and ``delevel``
(vs own recent pace). They are complementary: a print can be in-line with
consensus yet a big jump vs its own recent trend, or vice versa.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ...core.results import mean, safe_float, stdev


def _extract_level(raw: Any) -> Optional[float]:
    if isinstance(raw, dict):
        return safe_float(raw.get("value", raw.get("latest")))
    return safe_float(raw)


def _extract_previous(raw: Any) -> Optional[float]:
    if isinstance(raw, dict):
        return safe_float(raw.get("previous", raw.get("prior")))
    return None


def _series_from_block(raw: Any) -> List[float]:
    if not isinstance(raw, dict):
        return []
    hist = raw.get("history") or raw.get("series") or []
    return [v for v in (_extract_level(h) for h in hist) if v is not None]


def deltas(raw: Any) -> List[float]:
    """Return the period-over-period changes implied by a macro block.

    Prefers an explicit ``history`` series; otherwise uses the single
    ``previous``->``value`` change if present.
    """
    series = _series_from_block(raw)
    if len(series) >= 2:
        return [series[i] - series[i - 1] for i in range(1, len(series))]
    prev = _extract_previous(raw)
    cur = _extract_level(raw)
    if prev is not None and cur is not None:
        return [cur - prev]
    return []


def z_of_change(raw: Any, *, scale: Optional[float] = None,
                default_scale: float = 1.0) -> Dict[str, Any]:
    """Standardise the latest change against the dispersion of recent changes.

    Returns::

        {"value": float|None, "previous": float|None, "delta": float|None,
         "z": float|None, "direction": "up"|"down"|"flat"|None}
    """
    value = _extract_level(raw)
    previous = _extract_previous(raw)
    if value is None:
        return {"value": None, "previous": previous, "delta": None,
                "z": None, "direction": None}

    if previous is None:
        series = _series_from_block(raw)
        previous = series[-2] if len(series) >= 2 else None

    if previous is None:
        return {"value": value, "previous": None, "delta": None,
                "z": None, "direction": None}

    delta = value - previous
    history = deltas(raw)
    denom = safe_float(scale)
    if denom is None:
        denom = stdev(history) if len(history) >= 2 else None
    if denom is None or denom == 0:
        denom = safe_float(default_scale) or 1.0

    direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
    return {"value": value, "previous": previous, "delta": round(delta, 6),
            "z": round(delta / denom, 6), "direction": direction}


def delevel_scores(macro: Dict[str, Any],
                   *, scale_map: Optional[Dict[str, float]] = None
                   ) -> Dict[str, Dict[str, Any]]:
    """Apply :func:`z_of_change` to every numeric signal in a macro block."""
    scale_map = scale_map or {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, raw in (macro or {}).items():
        if key == "meta":
            continue
        if isinstance(raw, (int, float, dict)):
            out[key] = z_of_change(raw, scale=scale_map.get(key))
    return out


__all__ = ["z_of_change", "delevel_scores", "deltas"]