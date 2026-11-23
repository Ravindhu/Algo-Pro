"""
optim/constraints.py - feasibility checkers for a weight vector (pure).

``base.project_to_box`` *repairs* a weight vector toward feasibility; this module
*reports* whether a vector is feasible and by how much it differs. Separating the
two is deliberate: the repair is used in the hot path, the check is used in tests
and in the ``--explain`` report ("which constraint is binding?").

All functions return ``{"feasible": bool, "breaches": [str], "details": {...}}``
and never raise.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ConstraintSpec, gross_of, net_of
from ...core.results import safe_float

__all__ = ["check", "turnover", "beta_of", "describe_violations"]


def beta_of(weights: Dict[str, float], betas: Optional[Dict[str, float]]
            ) -> Optional[float]:
    """Portfolio beta (gross-normalised)."""
    if not weights or not betas:
        return None
    g = gross_of(weights) or 1.0
    return sum((safe_float(w) or 0.0) / g * (safe_float(betas.get(k)) or 1.0)
               for k, w in weights.items())


def turnover(target: Dict[str, float], current: Dict[str, float]) -> float:
    """One-way turnover between two weight dicts: ``sum|t-c| / 2``."""
    keys = set(target) | set(current)
    return sum(abs((safe_float(target.get(k)) or 0.0)
                   - (safe_float(current.get(k)) or 0.0)) for k in keys) / 2.0


def check(weights: Dict[str, float], spec: ConstraintSpec, *,
          current: Optional[Dict[str, float]] = None,
          betas: Optional[Dict[str, float]] = None
          ) -> Dict[str, Any]:
    """Verify a weight vector against every configured constraint."""
    breaches = []
    details: Dict[str, Any] = {}
    if not weights:
        return {"feasible": False, "breaches": ["empty weights"], "details": {}}

    # Box.
    for k, v in weights.items():
        fv = safe_float(v) or 0.0
        if fv < spec.min_weight - 1e-9 or fv > spec.max_weight + 1e-9:
            breaches.append(f"{k} outside box [{spec.min_weight}, {spec.max_weight}]")
    if spec.long_only and any((safe_float(v) or 0.0) < -1e-9 for v in weights.values()):
        breaches.append("negative weight (long-only)")

    # Gross / net.
    g = gross_of(weights)
    details["gross"] = round(g, 6)
    if g > spec.max_gross + 1e-9:
        breaches.append(f"gross {g:.4f} > max {spec.max_gross}")
    if g < spec.min_gross - 1e-9:
        breaches.append(f"gross {g:.4f} < min {spec.min_gross}")

    # Turnover.
    if spec.max_turnover is not None and current:
        to = turnover(weights, current)
        details["turnover"] = round(to, 6)
        if to > spec.max_turnover + 1e-9:
            breaches.append(f"turnover {to:.4f} > max {spec.max_turnover}")

    # Beta band.
    if (spec.beta_lower is not None or spec.beta_upper is not None) and betas:
        b = beta_of(weights, betas)
        details["beta"] = None if b is None else round(b, 6)
        if b is not None:
            if spec.beta_lower is not None and b < spec.beta_lower - 1e-9:
                breaches.append(f"beta {b:.4f} < lower {spec.beta_lower}")
            if spec.beta_upper is not None and b > spec.beta_upper + 1e-9:
                breaches.append(f"beta {b:.4f} > upper {spec.beta_upper}")

    return {"feasible": not breaches, "breaches": breaches, "details": details}


def describe_violations(result: Dict[str, Any]) -> str:
    """One-line human summary for logs."""
    if result.get("feasible"):
        return "feasible"
    return "; ".join(result.get("breaches") or ["infeasible"])