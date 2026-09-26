"""
ml/models/ensemble.py — combining the candidates, honestly.

THE RULE: AVERAGING IS A DECISION ABOUT TRUST, NOT A TRICK
----------------------------------------------------------
An ensemble is only worth anything when its members disagree SOMETIMES and
each one is independently right more often than not. Two failure modes:

  * members that are near-copies (same family, same features) — averaging
    them adds nothing, it just smooths the same signal;
  * members that are wrong in the SAME direction at the same time (all long
    in the same crash) — the correlation the desk actually cares about.

So this module does three things, none of them fancy:

  1. SIMPLE WEIGHTED AVERAGE of probability outputs. Weights are normalised
     and fixed at fit time on TRAIN folds only. No stacking, no meta-model —
     a meta-model on 750 rows is overfitting with extra steps, and its
     weights would be invisible to the audit. The weight vector IS the
     artifact, and it is readable.
  2. DISAGREEMENT TRACKING: the mean pairwise |p_i − p_j| is recorded, both
     overall and per fold. A high weight with a disagreement near zero is a
     signal that one member is being trusted for a duplicate.
  3. MIN-SKILL GATE: a member whose walk-forward skill on the TRAIN folds is
     below ``min_skill`` (default 0.0, i.e. better than the base rate) gets
     weight 0. A weak member in the mix is a rounding error in the wrong
     direction — out of principle, not out of tuning.

Weights are estimated with a tiny closed-form grid: normalise a positive
power p of each member's train-fold skill, for p in {0, 1, 2}. p=0 is the
equal-weight ensemble — the benchmark that simple averaging usually wins
with, and the reason we do not fit a logistic stacker.

Availability: pure Python, no torch/sklearn, so the ensembler can run even
when every torch-based member is unavailable (it averages the ones that are).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..evaluation.metrics import skill
from .base import ModelSpec, mean_proba

__all__ = ["build", "DEFAULT_PARAMS"]


DEFAULT_PARAMS: Dict[str, Any] = {
    "min_skill": 0.0,      # member train-fold skill must exceed this, else weight 0
    "power_grid": (0, 1, 2),   # skill^p weights; p=0 => equal weights
}


def build(**over: Any) -> ModelSpec:
    params = {**DEFAULT_PARAMS, **{k: v for k, v in over.items()
                                   if k in DEFAULT_PARAMS}}

    def _fit(members: Sequence[Tuple[str, List[float]]],
             y_true: Sequence[Optional[int]]) -> Dict[str, Any]:
        """members: (name, train_fold_probs) pairs — the probabilities each
        member produced over the TRAIN folds (same rows as ``y_true``),
        handed over by the walk-forward runner before any test prediction.

        The 2-tuple shape is deliberate: the ensembler never touches another
        model's internals, only its train-fold probability vector."""
        if len(members) < 2:
            raise ValueError("ensemble.fit: need >= 2 members")
        n = len(y_true)
        for name, probs in members:
            if len(probs) != n:
                raise ValueError(f"ensemble.fit: member '{name}' has "
                                 f"{len(probs)} probs vs {n} labels")

        skills: Dict[str, float] = {}
        for name, probs in members:
            s = skill(y_true, [1 if p >= 0.5 else 0 for p in probs])
            skills[name] = 0.0 if s is None else float(s)

        weights: Dict[str, float] = {}
        best_score = -float("inf")
        for p in params["power_grid"]:
            cand: Dict[str, float] = {}
            for name in skills:
                if skills[name] <= params["min_skill"]:
                    continue
                cand[name] = (skills[name] ** p) if p > 0 else 1.0
            tot = sum(cand.values())
            if tot <= 0:
                continue
            cand = {k: v / tot for k, v in cand.items()}
            w_list = [cand.get(name, 0.0) for name, _ in members]
            blended = mean_proba([probs for _, probs in members], w_list)
            sc = skill(y_true, [1 if b >= 0.5 else 0 for b in blended])
            sc = 0.0 if sc is None else float(sc)
            if sc > best_score:
                best_score = sc
                weights = cand

        dropped = [n2 for n2, s in skills.items() if n2 not in weights]
        return {"weights": weights, "skills": skills,
                "train_skill": best_score, "dropped": dropped,
                "disagreement": _mean_pairwise_disagreement(
                    [probs for _, probs in members])}

    def _predict(fitted: Dict[str, Any],
                 probs_by_member: Sequence[Sequence[float]]) -> List[float]:
        w = fitted["weights"]
        if not w:
            raise ValueError("ensemble.predict: no member passed the min-skill gate")
        if len(probs_by_member) != len(w):
            raise ValueError("ensemble.predict: member order/count must match fit")
        return mean_proba(probs_by_member, [w[name] for name in w])

    return ModelSpec(name="ensemble", family="ensemble",
                     fit_fn=_fit, predict_fn=_predict, params=params)


def _mean_pairwise_disagreement(probs_list: Sequence[Sequence[float]]) -> float:
    """Mean pairwise |p_i − p_j| across members, row by row."""
    n_members = len(probs_list)
    n_rows = len(probs_list[0])
    tot = 0.0
    pairs = 0
    for a in range(n_members):
        for b in range(a + 1, n_members):
            tot += sum(abs(probs_list[a][i] - probs_list[b][i])
                       for i in range(n_rows)) / n_rows
            pairs += 1
    return tot / pairs if pairs else 0.0
