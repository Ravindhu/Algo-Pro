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

  1. WEIGHTED MEAN of probability outputs, via :func:`base.mean_proba` — the
     one shared blending rule in the codebase. Weights come from a tiny
     closed-form grid over positive powers of each member's TRAIN-block skill
     (p ∈ {0,1,2}); p=0 is the equal-weight ensemble, which is the benchmark
     simple averaging usually wins with. No stacking, no meta-model: a
     meta-model on 750 rows is overfitting with extra steps, and the weight
     vector here IS the artifact — it is readable and auditable.
  2. DISAGREEMENT TRACKING: the mean pairwise |p_i − p_j| across members is
     recorded at fit time. A high weight with disagreement near zero means
     one member is being trusted for a duplicate of another.
  3. MIN-SKILL GATE: a member whose train-block skill is below ``min_skill``
     (default 0.0 = better than the base rate) gets weight 0. A weak member
     in the mix is a rounding error in the wrong direction.

THE MEMBERS ARE FITTED INTERNALLY
---------------------------------
Default members are ``ridge`` (regularised linear) and ``gbm`` (boosted trees):
chosen for CONTROLLED diversity, because they fail differently. The ensemble is
itself a candidate in the same search list as its members, so it must satisfy
the same ``fit_fn(X, y)`` contract — it fits its members on the train block and
estimates their weights from their TRAIN-block probabilities. No test row is
touched during weighting, so self-fitting introduces no leak. See ``build()``
for the bug this replaced.

When fewer than two member families are available (no scikit-learn means no
``gbm``), ``build()`` returns ``ok=False`` with the reason instead of claiming
to be usable and then failing inside every fold.

Availability: pure Python for the weighting logic, but the DEFAULT member set
needs scikit-learn (for ``gbm``). Without it the candidate honestly declines.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..evaluation.metrics import skill
from .base import ModelSpec, mean_proba

__all__ = ["build", "DEFAULT_PARAMS"]


DEFAULT_PARAMS: Dict[str, Any] = {
    "min_skill": 0.0,          # member train-fold skill must exceed this, else weight 0
    "power_grid": (0, 1, 2),   # skill^p weights; p=0 => equal weights
    #: Member families to fit internally. Chosen for CONTROLLED diversity:
    #: one regularised linear model and one tree-based non-linear model, which
    #: fail differently. Adding a third similar tree (RF next to GBM) would
    #: raise cost and correlation without adding information.
    "members": ("ridge", "gbm"),
}


def build(**over: Any) -> ModelSpec:
    params = {**DEFAULT_PARAMS, **{k: v for k, v in over.items()
                                   if k in DEFAULT_PARAMS}}
    member_names = params["members"]

    # Declare unavailability UP FRONT when too few member families can build.
    # Without this the ensemble advertised ok=True, then failed every fold with
    # a ValueError the fold runner caught — so the search reported "no edge"
    # for a candidate that never ran. Declining is honest; failing is not.
    from .base import has_sklearn
    available = []
    for _m in member_names:
        if _m == "ridge":
            available.append(_m)          # pure Python, always available
        elif _m in ("gbm", "rf") and has_sklearn():
            available.append(_m)
    if len(available) < 2:
        return ModelSpec(
            name="ensemble", family="ensemble", ok=False,
            params=params,
            reason=(f"needs >= 2 usable member families; only {available} "
                    f"available. Install scikit-learn (for gbm/rf) or use a "
                    f"single-family candidate. An ensemble of one model is "
                    f"just that model with extra steps."))

    def _fit(X: Sequence[Sequence[float]],
             y: Sequence[Optional[int]]) -> Dict[str, Any]:
        """Fit the member models on the SAME (X, y), then weight them.

        WHY THIS FITS ITS OWN MEMBERS
        -----------------------------
        An earlier version expected the caller to hand over
        ``(name, member_probs)`` pairs. That made the ensemble the one
        candidate in the search set that could not satisfy the runner's
        ``fit_fn(X, y)`` contract, so it advertised ``ok=True`` and then
        failed every fold. A candidate that cannot run is worse than one that
        declines: it burns a fold's worth of work and reports "no edge" in a
        way that looks like a finding.

        Fitting internally keeps the contract identical to every other
        family. The member weight estimation still uses train rows only —
        each member is fit on the train block, and its TRAIN-block
        probability is what feeds the skill weights. Test rows are never
        touched here, so no leak is introduced by the self-fitting.
        """
        from . import gradient_boosting, random_forest, ridge

        builders = {"ridge": ridge.build, "gbm": gradient_boosting.build,
                    "rf": random_forest.build}
        clean = [(xi, yi) for xi, yi in zip(X, y) if yi is not None]
        if len(clean) < 40:
            raise ValueError(f"ensemble.fit: {len(clean)} usable rows < 40")
        Xs = [list(map(float, xi)) for xi, _ in clean]
        ys = [int(yi) for _, yi in clean]

        # Fit each member; keep only those that actually work. A member whose
        # dependency vanished between build and fit must degrade to "unused",
        # not crash the ensemble.
        fitted: Dict[str, Any] = {}
        train_probs: Dict[str, List[float]] = {}
        for name in member_names:
            builder = builders.get(name)
            if builder is None:
                continue
            spec = builder()
            if not spec.ok:
                continue            # optional dep missing (e.g. no sklearn)
            try:
                m = spec.fit_fn(Xs, ys)
                if m is None:
                    continue
                p = spec.predict_fn(m, Xs)
            except Exception:       # noqa: BLE001 - a bad member must not
                continue            # kill the ensemble; it just is not used
            if len(p) != len(ys):
                continue
            fitted[name] = (spec, m)
            train_probs[name] = p

        if len(fitted) < 2:
            raise ValueError(
                f"ensemble.fit: only {len(fitted)} member(s) usable "
                f"({sorted(fitted)}) — need >= 2")

        # --- weight the members by their TRAIN-fold skill (no test leakage) --
        members = list(fitted.keys())
        skills: Dict[str, float] = {}
        for name in members:
            s = skill(ys, [1 if p >= 0.5 else 0 for p in train_probs[name]])
            skills[name] = 0.0 if s is None else float(s)

        probs_list = [train_probs[n] for n in members]
        weights: Dict[str, float] = {}
        best_score = -float("inf")
        for p in params["power_grid"]:
            cand: Dict[str, float] = {}
            for name in members:
                if skills[name] <= params["min_skill"]:
                    continue
                cand[name] = (skills[name] ** p) if p > 0 else 1.0
            tot = sum(cand.values())
            if tot <= 0:
                continue
            cand = {k: v / tot for k, v in cand.items()}
            blended = mean_proba(probs_list, [cand.get(n, 0.0) for n in members])
            sc = skill(ys, [1 if b >= 0.5 else 0 for b in blended])
            sc = 0.0 if sc is None else float(sc)
            # Tie-break: at equal skill prefer the SMALLER committee — a
            # dropped member means the gate earned its keep, and carrying a
            # skill-0 member for a tie is dead weight plus correlation risk.
            if sc > best_score or (sc == best_score and len(cand) < len(weights)):
                best_score = sc
                weights = cand

        # If the min-skill gate rejected everyone, fall back to equal weights
        # over all members. A committee of equally-weighted members still
        # beats no candidate at all, and the report will show it earned nothing.
        if not weights:
            weights = {n: 1.0 / len(members) for n in members}

        dropped = [n for n in members if n not in weights]
        return {"weights": weights, "skills": skills, "members": fitted,
                "member_order": members, "train_skill": best_score,
                "dropped": dropped,
                "disagreement": _mean_pairwise_disagreement(probs_list)}

    def _predict(fitted: Dict[str, Any],
                 X: Sequence[Sequence[float]]) -> List[float]:
        """Blend the members' predictions on new rows, in the fitted order."""
        w = fitted["weights"]
        if not w:
            raise ValueError("ensemble.predict: no member passed the min-skill gate")
        order = fitted["member_order"]
        Xs = [list(map(float, r)) for r in X]
        cols = []
        used = []
        for name in order:
            spec, m = fitted["members"][name]
            cols.append(spec.predict_fn(m, Xs))
            used.append(name)
        return mean_proba(cols, [w.get(n, 0.0) for n in used])

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
