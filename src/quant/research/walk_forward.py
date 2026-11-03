"""Walk-forward folds with a purge gap before each test block. Thin wrapper over Blue_engine.

Flow stage: Walk-forward + purged CV
Status: PHASE 0 IMPLEMENTED (wrapper: fold geometry comes from Blue_engine)
Existing code to reuse or wrap (do not copy): Blue_engine/ml/evaluation/splits.py (walk_forward)

WHAT CHANGED IN THE WRAP (API change, flagged in docs/PHASE0_REUSE_AUDIT.md)
-----------------------------------------------------------------------------
- Blue derives the test-block size from ``n`` and ``n_splits``. The old function
  took ``test_size``. Blue has no such input, so the parameter is removed.
- Blue skips folds whose training block is shorter than ``min_train``. The old
  function did the same. Default kept at 1 to match the old behaviour.
- The purge is unchanged in meaning: ``purge`` bars just before each test block
  are removed from training.
- The only production-adjacent caller was a unit test. Scripts already call
  Blue_engine directly (scripts/diagnostics/diag3_predictions.py).
"""
from __future__ import annotations

from typing import Iterator, Tuple

import numpy as np

from quant.core.blue_bridge import load


def purged_walk_forward(
    n: int,
    n_splits: int,
    purge: int = 0,
    min_train: int = 1,
    mode: str = "expanding",
) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    """Yield (train_idx, test_idx) index arrays for each fold, oldest first.

    Training rows always come before the test block, with ``purge`` rows dropped
    between them. The index layout is Blue_engine's, not a copy of it.
    """
    splitter = load("ml.evaluation.splits").walk_forward
    # Blue reads timestamps only for the manifest; placeholder rows are enough here.
    splits = splitter(
        [{}] * n,
        n_splits=n_splits,
        horizon=purge,
        embargo=0,
        mode=mode,
        min_train=min_train,
    )
    for split in splits:
        yield np.asarray(split.train_idx, dtype=int), np.asarray(split.test_idx, dtype=int)
