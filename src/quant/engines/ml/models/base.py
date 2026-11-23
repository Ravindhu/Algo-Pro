"""
ml/models/base.py — the contract every model family obeys.

WHY A CONTRACT (and not "just import sklearn")
----------------------------------------------
AGENTS.md asks for many model families — ridge, gradient boosting, random
forest, neural nets, CNNs, reinforcement learning. Each has a different
library, a different failure mode, and a different "unavailable" story. Without
a single contract you get eight slightly different `fit`/`predict` shapes and
the evaluator silently breaks on the eighth.

The contract is TWO CALLABLES, matching :mod:`ml.evaluation.walk_forward`:

    build(...) -> ModelSpec where ModelSpec.fit_fn(X, y) -> fitted
                                  and ModelSpec.predict_fn(fitted, X) -> [p, ...]

Everything downstream (walk-forward, registry, dashboard) sees the same shape,
so a candidate swap is a one-line config change, not a refactor.

THE ONE HONESTY RULE: never fabricate a probability
---------------------------------------------------
If the model is unavailable (sklearn not installed, torch not installed, not
enough data), ``build()`` returns ``ok=False`` with a REASON — and the trainer
records that reason and refuses to train. A model that returned 0.5 for every
row would produce a *beautiful-looking* fold report with zero information.
Returning a real "not available" is the whole point of ``ok``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import logger

__all__ = ["ModelSpec", "FitFn", "PredictFn", "mean_proba"]


FitFn = Callable[..., Any]
PredictFn = Callable[..., Any]


@dataclass
class ModelSpec:
    """A candidate model, ready to be handed to the walk-forward runner.

    Attributes:
        name: stable identifier (goes in the manifest and the registry).
        family: coarse class — "linear" | "boosted_trees" | "bagged_trees" |
            "neural_net" | "cnn" | "rl" | "ensemble". The dashboard groups by
            this so the operator can see WHICH families actually pass.
        ok: False means "cannot be built on this machine" — never a silent
            fallback. The caller records ``reason`` and skips the candidate.
        reason: the human explanation when ``ok`` is False.
        fit_fn / predict_fn: the two callables. Never None when ``ok``.
        params: the exact hyperparameters, hashed into the manifest.
        n_params_estimate: rough model complexity (used by the deflation
            audit to flag " searched a huge grid on a small sample").
    """

    name: str
    family: str
    fit_fn: Optional[FitFn] = None
    predict_fn: Optional[PredictFn] = None
    params: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    reason: Optional[str] = None
    n_params_estimate: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Manifest-shaped dict (functions dropped — they are not JSON-able)."""
        return {"name": self.name, "family": self.family,
                "ok": self.ok, "reason": self.reason,
                "params": dict(self.params),
                "n_params_estimate": self.n_params_estimate}


def mean_proba(rows: Sequence[Sequence[float]], weights: Optional[Sequence[float]] = None
               ) -> List[float]:
    """The (only) blending rule the ensemble uses — a weighted mean.

    Kept trivial ON PURPOSE: an ensemble's power is in the DIVERSITY of its
    members, not in a clever blending rule. A complicated blender on five
    members is five extra overfitting opportunities.
    """
    if not rows:
        return []
    if weights is None:
        weights = [1.0] * len(rows)
    total = sum(weights) or 1.0
    out: List[float] = []
    n_members = len(rows)
    for j in range(len(rows[0])):
        acc = 0.0
        for i in range(n_members):
            try:
                acc += float(rows[i][j]) * (weights[i] / total)
            except (TypeError, ValueError, IndexError):
                continue
        out.append(max(0.0, min(1.0, acc)))
    return out


def _as_floats(X: Sequence[Sequence[float]]) -> Tuple[List[List[float]], int]:
    """Coerce a feature matrix to plain floats, dropping unusable rows.

    Returns (clean_X, n_dropped). Dropped rows are NOT silently repaired to
    zeros — the caller's label list is aligned to the ORIGINAL X, so the
    trainer is the one that must drop (it knows both lists). This function is
    a helper for model internals, where a single bad row must not kill a fold.
    """
    clean: List[List[float]] = []
    dropped = 0
    for row in X:
        try:
            vals = [float(v) for v in row]
            if all(v == v and abs(v) != float("inf") for v in vals):
                clean.append(vals)
                continue
            dropped += 1
        except (TypeError, ValueError):
            dropped += 1
    return clean, dropped


def has_sklearn() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def has_torch() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False
