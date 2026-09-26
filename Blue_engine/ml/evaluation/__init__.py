"""
ml/evaluation — how we decide whether a model is worth anything.

Modules
-------
splits    purged walk-forward / purged K-fold (the only causal splitters)
metrics   accuracy, AUC, calibration, Sharpe, deflated Sharpe, cost-adjusted edge
walk_forward  the orchestrator that runs folds and aggregates honestly

THE ORDER OF OPERATIONS THAT MATTERS
------------------------------------
Most quant pipelines run:  fit -> score -> if good, ship.

The order here is deliberately different:

    1. split       (purged walk-forward — decided BEFORE any model exists)
    2. audit       (leakage + quality — BEFORE fitting, not after)
    3. fit         (one fold at a time, on train only)
    4. score       (skill vs base rate, per fold)
    5. aggregate   (mean AND std — dispersion is the risk number)
    6. deflate     (correct for how many variants you tried)
    7. cost-check  (does the edge survive Indian transaction costs)
    8. only then   consider shipping

Steps 2, 6 and 7 are the ones usually skipped, and they are the three that
actually prevent losses. A model that passes 1-5 and fails 6 or 7 is a model that
looked good in a notebook.

WHY NOTHING HERE IMPORTS sklearn/torch
--------------------------------------
The evaluation layer must run wherever a model ran, including a bare machine
auditing someone else's artifact. It also keeps every formula open to inspection —
you can verify the AUC implementation against the Mann-Whitney definition by
reading it, which you cannot do with a library call. Sample sizes here are small
(fold-level), so pure Python costs nothing.
"""

from __future__ import annotations

from . import metrics, splits
from .metrics import (
    auc, base_rate, brier_score, cost_adjusted_edge, deflated_sharpe,
    fold_summary, hit_rate, max_drawdown, precision_recall, sharpe, skill,
)
from .splits import Split, describe_splits, last_holdout, purged_kfold, walk_forward

__all__ = [
    # submodules
    "splits", "metrics",
    # splits
    "Split", "walk_forward", "purged_kfold", "last_holdout", "describe_splits",
    # metrics
    "auc", "base_rate", "brier_score", "cost_adjusted_edge", "deflated_sharpe",
    "fold_summary", "hit_rate", "max_drawdown", "precision_recall", "sharpe",
    "skill",
]