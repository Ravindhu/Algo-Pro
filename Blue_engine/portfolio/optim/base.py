"""
optim/base.py - the optimiser contract shared by every optimiser.

Design (mirrors ``core/base.py`` for engines)
---------------------------------------------
An optimiser takes a **problem** and returns a **solution**. Both are plain
dicts so optimisers are interoperable, serialisable, testable with no I/O, and
swappable behind a registry.

Problem shape::

    {
      "universe": ["NIFTY", "BANKNIFTY", ...],
      "cov":      [[...]],            # covariance matrix (annualised)
      "vols":     {"NIFTY": 0.14, ...},        # optional (derived from cov)
      "mu":       {"NIFTY": 0.08, ...},        # expected returns (optional)
      "capital":  1_000_000.0,                 # optional
      "constraints": { ...ConstraintSpec... }, # optional
      "current_weights": {"NIFTY": 0.3, ...},  # optional (for turnover)
      "betas":    {"NIFTY": 1.0, ...},         # optional (for beta band)
    }

Solution shape::

    {
      "ok": bool,
      "weights": {"NIFTY": 0.31, ...},
      "method": "risk_parity",
      "diagnostics": { ... },
      "warnings": [str],
    }

The optimiser NEVER raises: a failure is ``ok=False`` + a warning.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ...core.results import clamp, safe_float

logger = logging.getLogger(__name__)

__all__ = ["ConstraintSpec", "BaseOptimiser", "project_to_box",
           "normalise_weights", "gross_of", "weights_to_list", "list_to_weights"]


@dataclass
class ConstraintSpec:
    """The feasible set for a weight vector.

    Attributes
    ----------
    min_weight / max_weight : per-sleeve box.
    max_gross / min_gross   : bounds on ``sum(|w|)``.
    max_turnover            : one-way turnover cap vs ``current_weights``.
    beta_lower / beta_upper : portfolio-beta band (needs ``betas`` in problem).
    long_only               : force w_i >= 0 (default True).
    """

    min_weight: float = 0.0
    max_weight: float = 1.0
    max_gross: float = 1.0
    min_gross: float = 0.0
    max_turnover: Optional[float] = None
    beta_lower: Optional[float] = None
    beta_upper: Optional[float] = None
    long_only: bool = True

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ConstraintSpec":
        d = d or {}
        return cls(
            min_weight=float(d.get("min_weight", 0.0)),
            max_weight=float(d.get("max_weight", 1.0)),
            max_gross=float(d.get("max_gross", 1.0)),
            min_gross=float(d.get("min_gross", 0.0)),
            max_turnover=(None if d.get("max_turnover") is None
                          else float(d["max_turnover"])),
            beta_lower=(None if d.get("beta_lower") is None
                        else float(d["beta_lower"])),
            beta_upper=(None if d.get("beta_upper") is None
                        else float(d["beta_upper"])),
            long_only=bool(d.get("long_only", True)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_weight": self.min_weight, "max_weight": self.max_weight,
            "max_gross": self.max_gross, "min_gross": self.min_gross,
            "max_turnover": self.max_turnover, "beta_lower": self.beta_lower,
            "beta_upper": self.beta_upper, "long_only": self.long_only,
        }


class BaseOptimiser(abc.ABC):
    """Abstract optimiser.

    Subclasses implement :meth:`_optimise` returning a raw weight list; the base
    class applies the shared post-processing (box clamp, gross scaling, turnover
    projection) so constraint handling is consistent across methods.
    """

    name: str = "base"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.config: Dict[str, Any] = dict(config or {})
        self._log_fn = log

    def log(self, message: str) -> None:
        if self._log_fn is not None:
            self._log_fn(f"[optim:{self.name}] {message}")
        else:
            logger.info("[optim:%s] %s", self.name, message)

    # ---- public API ----------------------------------------------------- #
    def optimise(self, problem: Dict[str, Any]) -> Dict[str, Any]:
        """Run the optimiser. NEVER raises."""
        universe = list(problem.get("universe") or [])
        if not universe:
            return {"ok": False, "weights": {}, "method": self.name,
                    "diagnostics": {}, "warnings": ["empty universe"]}
        spec = ConstraintSpec.from_dict(problem.get("constraints"))
        try:
            raw = self._optimise(problem, spec)
        except Exception as exc:  # noqa: BLE001 - never halt
            self.log(f"optimise FAILED: {exc!r}")
            return {"ok": False, "weights": {}, "method": self.name,
                    "diagnostics": {}, "warnings": [f"{self.name}: {exc!r}"]}
        if not raw:
            return {"ok": False, "weights": {}, "method": self.name,
                    "diagnostics": {}, "warnings": ["optimiser produced no weights"]}
        weights = list_to_weights(universe, raw)
        # Enforce long-only before the box projection (so a negative never slips
        # through a min_weight=0 clamp with an accidental flip).
        if spec.long_only:
            weights = {k: max(0.0, v) for k, v in weights.items()}
        projected, diag = project_to_box(
            weights, spec,
            current=problem.get("current_weights"),
            betas=problem.get("betas"))
        diag["method"] = self.name
        self.log(f"weights={_fmt(projected)} gross={gross_of(projected):.4f}")
        return {"ok": True, "weights": projected, "method": self.name,
                "diagnostics": diag, "warnings": list(diag.get("warnings", []))}

    @abc.abstractmethod
    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        """Return a raw weight list (same order as ``problem['universe']``)."""
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "config_keys": sorted(self.config.keys())}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def normalise_weights(raw: Sequence[float], *, gross: float = 1.0
                      ) -> List[float]:
    """Scale weights so ``sum(|w|) == gross`` (None-safe, zero-safe)."""
    ws = [safe_float(x) or 0.0 for x in raw]
    total = sum(abs(x) for x in ws)
    if total <= 0:
        n = len(ws)
        return [gross / n] * n if n else []
    return [x / total * gross for x in ws]


def weights_to_list(weights: Dict[str, float],
                    universe: Sequence[str]) -> List[float]:
    return [safe_float(weights.get(k)) or 0.0 for k in universe]


def list_to_weights(universe: Sequence[str], raw: Sequence[float]
                    ) -> Dict[str, float]:
    return {u: float(raw[i]) for i, u in enumerate(universe) if i < len(raw)}


def gross_of(weights: Dict[str, float]) -> float:
    return sum(abs(safe_float(v) or 0.0) for v in weights.values())


def net_of(weights: Dict[str, float]) -> float:
    return sum(safe_float(v) or 0.0 for v in weights.values())


def project_to_box(weights: Dict[str, float], spec: ConstraintSpec, *,
                   current: Optional[Dict[str, float]] = None,
                   betas: Optional[Dict[str, float]] = None
                   ) -> tuple:
    """Project a weight dict onto the feasible set (best-effort, deterministic).

    Order of operations (documented so a reader can trace a surprising weight):

    1. Box clamp each weight to ``[min_weight, max_weight]``.
    2. Renormalise to ``min(max_gross, ...)`` so gross is within bounds.
    3. If a beta band is set and ``betas`` given, tilt the largest contributors
       to pull beta into range (a cheap, transparent repair).
    4. If ``max_turnover`` is set and ``current`` given, shrink the move toward
       the *current* book until turnover is within the cap.

    Returns ``(weights, diagnostics)`` where diagnostics records which step was
    binding — this is the ``--explain`` payload.
    """
    diag: Dict[str, Any] = {"binding": [], "warnings": []}
    if not weights:
        return {}, diag

    # 1. Box.
    boxed = {k: clamp(safe_float(v) or 0.0, spec.min_weight, spec.max_weight)
             for k, v in weights.items()}
    if any((safe_float(weights[k]) or 0.0) != boxed[k] for k in weights):
        diag["binding"].append("box")

    # 2. Gross.
    g = gross_of(boxed)
    target_gross = clamp(g, spec.min_gross, spec.max_gross)
    if target_gross > 0 and g > 0 and abs(g - target_gross) > 1e-9:
        f = target_gross / g
        boxed = {k: v * f for k, v in boxed.items()}
        diag["binding"].append("gross")
    diag["gross"] = round(gross_of(boxed), 6)

    # 3. Beta band (only if betas supplied and a band is configured).
    if (spec.beta_lower is not None or spec.beta_upper is not None) and betas:
        boxed = _repair_beta(boxed, betas, spec, diag)

    # 4. Turnover.
    if spec.max_turnover is not None and current:
        boxed = _repair_turnover(boxed, current, spec, diag)

    # Re-apply box after repairs (repairs must not breach the box).
    boxed = {k: clamp(v, spec.min_weight, spec.max_weight)
             for k, v in boxed.items()}
    # Round for reporting, then repair the sub-1e-8 rounding residue so the
    # emitted gross can NEVER exceed max_gross (a downstream risk gate must not
    # trip on a floating-point artefact like 1.00000001).
    rounded = {k: round(v, 8) for k, v in boxed.items()}
    g_rounded = gross_of(rounded)
    if g_rounded > spec.max_gross and rounded:
        f = spec.max_gross / g_rounded
        rounded = {k: round(v * f, 8) for k, v in rounded.items()}
        # One more pass: rounding again may leave a stray 1e-8; trim it off the
        # largest non-zero sleeve so the invariant holds exactly.
        excess = gross_of(rounded) - spec.max_gross
        if excess > 0:
            anchor = max(rounded, key=lambda k: rounded[k])
            rounded[anchor] = round(rounded[anchor] - excess, 8)
            if rounded[anchor] <= 0:
                rounded[anchor] = 0.0
    return rounded, diag


def _repair_beta(weights: Dict[str, float], betas: Dict[str, float],
                 spec: ConstraintSpec, diag: Dict[str, Any]
                 ) -> Dict[str, float]:
    """Cheaply pull portfolio beta into [beta_lower, beta_upper].

    Strategy: blend toward the *minimum-beta* asset when beta is too high, and
    toward the *maximum-beta* asset when too low, in 10% steps (max 8 steps).
    Transparent and monotone; not an optimum, but it always terminates and never
    needs a solver.
    """
    lo, hi = spec.beta_lower, spec.beta_upper
    keys = list(weights.keys())
    if not keys:
        return weights

    def _beta(w: Dict[str, float]) -> float:
        g = gross_of(w) or 1.0
        return sum(w[k] / g * (safe_float(betas.get(k)) or 1.0) for k in w)

    for _ in range(8):
        b = _beta(weights)
        if lo is not None and b < lo:
            anchor = max(keys, key=lambda k: safe_float(betas.get(k)) or 1.0)
        elif hi is not None and b > hi:
            anchor = min(keys, key=lambda k: safe_float(betas.get(k)) or 1.0)
        else:
            break
        for k in weights:
            weights[k] = weights[k] * 0.9 + (weights[anchor] if k == anchor else 0.0)
        # Re-normalise to preserve gross so the beta move is scale-neutral.
        weights = dict(zip(keys, normalise_weights(
            [weights[k] for k in keys], gross=gross_of(weights))))
    diag["binding"].append("beta")
    diag["beta"] = round(_beta(weights), 6)
    return weights


def _repair_turnover(weights: Dict[str, float], current: Dict[str, float],
                     spec: ConstraintSpec, diag: Dict[str, Any]
                     ) -> Dict[str, float]:
    """Shrink the move toward ``current`` until one-way turnover is within cap."""
    cap = spec.max_turnover
    if cap is None or cap <= 0:
        return weights

    def _turnover(target: Dict[str, float]) -> float:
        keys = set(target) | set(current)
        return sum(abs((safe_float(target.get(k)) or 0.0)
                       - (safe_float(current.get(k)) or 0.0)) for k in keys) / 2.0

    to = _turnover(weights)
    if to <= cap:
        return weights
    # Binary-search the blend alpha in [0,1]: w = a*target + (1-a)*current.
    lo, hi = 0.0, 1.0
    for _ in range(30):
        mid = (lo + hi) / 2.0
        blend = {k: mid * (safe_float(weights.get(k)) or 0.0)
                 + (1 - mid) * (safe_float(current.get(k)) or 0.0)
                 for k in set(weights) | set(current)}
        if _turnover(blend) <= cap:
            lo = mid
        else:
            hi = mid
    blend = {k: lo * (safe_float(weights.get(k)) or 0.0)
             + (1 - lo) * (safe_float(current.get(k)) or 0.0)
             for k in set(weights) | set(current)}
    diag["binding"].append("turnover")
    diag["turnover_before"] = round(to, 6)
    diag["turnover_after"] = round(_turnover(blend), 6)
    return blend


def _fmt(weights: Dict[str, float]) -> str:
    return "{" + ", ".join(f"{k}:{v:.3f}" for k, v in sorted(weights.items())) + "}"