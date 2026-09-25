"""
regime.py - band the composite into a label and a gross-exposure multiplier.

Two responsibilities, both pure:

  1. **Banding** - map the composite risk-on score in [-1, 1] to a categorical
     regime (RISK_ON ... RISK_OFF) with desk guidance. Cut-points are the ones
     already used by the baseline engine (backwards compatible).
  2. **Multiplier** - translate a score *or a model posterior* into the
     ``gross_exposure_multiplier`` that the Portfolio engine consumes.

Two multiplier paths
--------------------
* ``score_multiplier``  - ``1 + score``, clamped [0.25, 1.5]. The baseline path.
* ``posterior_multiplier`` - use ``P(RISK_ON)`` from an HMM/cluster to size,
  which is *confidence-aware*: a 0.5 posterior is neutral, a 0.9 posterior is
  strong. This is the model-driven upgrade (plan section 6).

A ``blend`` mixes the two so the desk can trust the model progressively.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..core.results import clamp, safe_float

#: (threshold, label, guidance) - same cut-points as the baseline engine.
REGIME_BANDS = (
    (0.35, "RISK_ON", "Expansionary: raise gross exposure, favour beta."),
    (0.10, "MILD_RISK_ON", "Constructive: hold, add on dips."),
    (-0.10, "NEUTRAL", "Balanced: neutral gross, tighten selection."),
    (-0.35, "MILD_RISK_OFF", "Defensive tilt: trim beta, raise quality."),
    (-2.00, "RISK_OFF", "Contractionary: cut gross, hedge, hold cash."),
)

MULTIPLIER_FLOOR = 0.25
MULTIPLIER_CAP = 1.5


def band(score: float) -> Tuple[str, str]:
    """Map a composite score to (label, guidance)."""
    for threshold, label, guidance in REGIME_BANDS:
        if score >= threshold:
            return label, guidance
    return "RISK_OFF", "Contractionary: cut gross, hedge, hold cash."


def score_multiplier(score: float) -> float:
    """Baseline path: 1 + score, clamped."""
    return clamp(1.0 + float(score), MULTIPLIER_FLOOR, MULTIPLIER_CAP)


def posterior_multiplier(p_risk_on: float, *,
                         floor: float = MULTIPLIER_FLOOR,
                         cap: float = MULTIPLIER_CAP) -> float:
    """Confidence-aware path: size linearly in P(RISK_ON) in [0, 1].

    P=0.5 -> neutral (1.0x). P=1 -> ``cap``. P=0 -> ``floor``. This is a simple,
    defensible linear map; the desk can reinterpret it as a Kelly-lite sizing
    rule where ``P(RISK_ON)`` is the edge.
    """
    p = clamp(safe_float(p_risk_on, 0.5), 0.0, 1.0)
    if p is None:
        p = 0.5
    # Linear from floor..cap centred so p=0.5 -> 1.0.
    if p >= 0.5:
        return clamp(1.0 + (p - 0.5) * 2.0 * (cap - 1.0), 1.0, cap)
    return clamp(1.0 - (0.5 - p) * 2.0 * (1.0 - floor), floor, 1.0)


def combine_multiplier(score: float, p_risk_on: Optional[float], *,
                       model_weight: float = 0.0) -> Dict[str, Any]:
    """Blend the score path and the posterior path.

    ``model_weight`` in [0,1] is how much to trust the model posterior. 0 (the
    default) reproduces the baseline exactly - so nothing changes until the desk
    opts in. 1 fully defers to the model. The result is always clamped.
    """
    m_score = score_multiplier(score)
    if p_risk_on is None:
        return {"multiplier": m_score, "path": "score",
                "score_multiplier": m_score, "posterior_multiplier": None}
    m_post = posterior_multiplier(p_risk_on)
    w = clamp(safe_float(model_weight, 0.0) or 0.0, 0.0, 1.0)
    blended = (1.0 - w) * m_score + w * m_post
    path = "score" if w == 0 else ("posterior" if w == 1 else "blend")
    return {"multiplier": clamp(blended, MULTIPLIER_FLOOR, MULTIPLIER_CAP),
            "path": path, "score_multiplier": round(m_score, 4),
            "posterior_multiplier": round(m_post, 4),
            "model_weight": w}


__all__ = [
    "REGIME_BANDS", "band", "score_multiplier", "posterior_multiplier",
    "combine_multiplier", "MULTIPLIER_FLOOR", "MULTIPLIER_CAP",
]