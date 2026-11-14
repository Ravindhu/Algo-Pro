"""Other optimizers wrapper: risk parity, inverse vol, min-CVaR, min variance, DELEGATED to Blue_engine.

Flow stage: Other optimizers
Status: PHASE 5 IMPLEMENTED (wrap; candidates only; HRP stays local)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/portfolio/optim/risk_parity.py,
src/quant/blue_engine/portfolio/optim/cvar.py, src/quant/blue_engine/portfolio/optim/mean_variance.py

These produce candidate weights. The sizing stage picks what to use, and the portfolio
risk limits still apply. HRP stays on our local implementation, because Blue's HRP
disagrees with the published algorithm (audit X-25).

FAIL-CLOSED RULES
-----------------
- Every result must be long-only (no negative weight) and sum to 1 within 1e-6.
  Otherwise the result is refused (``ok=False``, no weights).
- Blue unavailable or a computation raises: ``ok=False``.
- Never raises.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["risk_parity", "inverse_vol", "min_cvar", "min_variance"]

_TOL = 1e-6


def _checked(weights: Any) -> Dict[str, Any]:
    if not isinstance(weights, list) or not weights:
        return {"ok": False, "weights": [], "reason": "optimizer returned no weights"}
    try:
        vals = [float(w) for w in weights]
    except (TypeError, ValueError):
        return {"ok": False, "weights": [], "reason": "weights are not numbers"}
    if any((not math.isfinite(v)) or v < 0 for v in vals):
        return {"ok": False, "weights": [], "reason": "negative or non-finite weight (long-only)"}
    if abs(sum(vals) - 1.0) > _TOL:
        return {"ok": False, "weights": [], "reason": "weights do not sum to 1"}
    return {"ok": True, "weights": vals, "reason": ""}


def _call(module: str, fn_name: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        fn: Callable[..., Any] = getattr(load(module), fn_name)
    except BlueEngineUnavailable as exc:
        return {"ok": False, "weights": [], "reason": f"Blue optimizer unavailable: {exc}"}
    try:
        out = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "weights": [], "reason": f"optimizer failed: {exc!r}"}
    return _checked(out)


def risk_parity(cov: List[List[float]], *, iterations: int = 500, tol: float = 1e-10) -> Dict[str, Any]:
    return _call("portfolio.optim.risk_parity", "erc_weights", cov, iterations=iterations, tol=tol)


def inverse_vol(vols: List[float]) -> Dict[str, Any]:
    return _call("portfolio.optim.risk_parity", "inverse_vol_weights", vols)


def min_cvar(cov_or_scen: Any, *, level: float = 0.95, mu: Any = None,
             risk_aversion: float = 0.0) -> Dict[str, Any]:
    return _call("portfolio.optim.cvar", "mean_cvar_weights", cov_or_scen,
                 level=level, mu=mu, risk_aversion=risk_aversion)


def min_variance(cov: List[List[float]], *, ridge: float = 0.0) -> Dict[str, Any]:
    return _call("portfolio.optim.mean_variance", "min_variance_weights", cov, ridge=ridge)
