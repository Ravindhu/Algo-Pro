"""Auto-tune wrapper: recompute the OOS approval gate, never promote from here.

Flow stage: Champion/challenger, Re-opt request, Model registry
Status: PHASE 5 IMPLEMENTED (wrap; shadow only - promotion stays a human act)
Existing code to reuse or wrap (do not copy): Blue_engine/core/auto_tune.py
(approval_ok, AutoTuneConfig, AutoTuner, TuneResult)

WHAT IS WRAPPED AND WHAT IS NOT
-------------------------------
``approval_ok`` is wrapped: it re-derives the approval from the raw batch report
instead of trusting an ``approved`` boolean, which is exactly the "do not trust
the optimiser" rule this project needs. Re-implementing those comparisons here
would create a second, drifting copy of the same policy.

``AutoTuner`` is deliberately NOT constructed by this module. In ``apply`` mode
it rewrites the live strategy config. AGENTS.md rule 5 says only approved models
enter production and a person signs off. So:

- ``review`` answers "would this batch report pass?" and records the answer.
- A report that asks to be applied is REFUSED by this wrapper and the refusal is
  returned as data, not raised.
- Installing parameters stays with a human using the research runner.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["AutoTuneUnavailable", "DEFAULT_POLICY", "review_batch", "review_candidates"]

#: The approval policy, mirrored from Blue's AutoTuneConfig defaults. Pass a
#: different dict to tighten it; nothing here loosens the numbers.
DEFAULT_POLICY: Dict[str, float] = {
    "min_oos_sharpe": 1.0,
    "oos_is_ratio": 0.7,
    "min_dsr_probability": 0.95,
    "max_dd_ratio": 1.3,
}


class AutoTuneUnavailable(RuntimeError):
    """Blue's auto-tune policy cannot be loaded."""


def _approval_fn():
    try:
        return load("core.auto_tune").approval_ok
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise AutoTuneUnavailable(f"Blue auto_tune unavailable: {exc}") from exc


def _policy(policy: Optional[Dict[str, Any]]) -> Dict[str, float]:
    out = dict(DEFAULT_POLICY)
    for key in DEFAULT_POLICY:
        if policy and policy.get(key) is not None:
            value = float(policy[key])
            if value != value:
                raise ValueError(f"{key} must be a finite number")
            out[key] = value
    if out["oos_is_ratio"] < 0 or out["oos_is_ratio"] > 1:
        raise ValueError("oos_is_ratio must be in [0, 1]")
    if out["min_dsr_probability"] < 0 or out["min_dsr_probability"] > 1:
        raise ValueError("min_dsr_probability must be in [0, 1]")
    return out


def review_batch(report: Dict[str, Any], policy: Optional[Dict[str, Any]] = None,
                 *, audit=None, strategy_id: str = "") -> Dict[str, Any]:
    """Would this optimiser batch report pass the out-of-sample policy?

    Returns ``{approved, reason, policy, applied_by_this_wrapper: False}``.
    A ``mode: apply`` request inside ``report`` is refused: this wrapper never
    changes a live parameter file.
    """
    fn = _approval_fn()
    pol = _policy(policy)
    approved, reason = fn(report, min_oos_sharpe=pol["min_oos_sharpe"],
                          oos_is_ratio=pol["oos_is_ratio"],
                          min_dsr_probability=pol["min_dsr_probability"],
                          max_dd_ratio=pol["max_dd_ratio"])
    if isinstance(report, dict) and str(report.get("mode", "")).lower() == "apply":
        approved, reason = False, ("refused: promotion requests are not accepted "
                                   "from the advice wrapper; a person installs parameters")
    out = {"approved": bool(approved), "reason": str(reason), "policy": pol,
           "applied_by_this_wrapper": False, "advice_only": True,
           "strategy_id": strategy_id}
    if audit is not None:
        audit.append("auto_tune_review", out)
    return out


def review_candidates(reports: List[Dict[str, Any]],
                      policy: Optional[Dict[str, Any]] = None,
                      *, audit=None) -> List[Dict[str, Any]]:
    """``review_batch`` over several reports. One bad report cannot hide others."""
    rows: List[Dict[str, Any]] = []
    for item in reports or []:
        strategy_id = str(item.get("strategy_id", "")) if isinstance(item, dict) else ""
        rows.append(review_batch(item, policy, audit=audit, strategy_id=strategy_id))
    return rows
