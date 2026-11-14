"""Walk-forward folds with a purge gap before each test block. Local code (no Blue import).

Flow stage: Walk-forward + purged CV
Status: PHASE 1 IMPLEMENTED (stage 2 port 2026-11-06: fold geometry is local)
Ported from: src/quant/blue_engine/ml/evaluation/splits.py (walk_forward), same layout rules.
Parity against Blue: tests/parity/test_walk_forward_parity.py (runs where Blue loads or
via the isolated loader).

LAYOUT (same as Blue)
---------------------
- Reserve the last test block; split the rest into ``n_splits`` equal blocks of size
  ``n // (n_splits + 1)`` (integer floor, so every test block is the same size).
- Train block ends ``purge`` rows before the test block (the purge gap).
- ``expanding``: train starts at row 0. ``rolling``: train is the last ``min_train`` rows.
- Folds whose training block is shorter than ``min_train`` are skipped.

PURGE
-----
``purge`` must equal the label's forward horizon in bars. A wrong value re-introduces leakage.
"""
from __future__ import annotations

import logging
from typing import Iterator, Tuple

import numpy as np

logger = logging.getLogger(__name__)


def purged_walk_forward(
    n: int,
    n_splits: int,
    purge: int = 0,
    min_train: int = 1,
    mode: str = "expanding",
) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    """Yield (train_idx, test_idx) index arrays for each fold, oldest first."""
    if n < 0:
        raise ValueError("n must be >= 0")
    if n_splits < 1:
        raise ValueError("n_splits must be >= 1")
    if purge < 0 or min_train < 0:
        raise ValueError("purge and min_train must be >= 0")
    if n == 0:
        logger.warning("purged_walk_forward: empty dataset")
        return

    test_size = max(1, n // (n_splits + 1))
    produced = 0
    for k in range(n_splits):
        test_start = n - (n_splits - k) * test_size
        test_end = test_start + test_size
        if test_start <= 0 or test_end > n:
            continue

        train_end = max(0, test_start - purge)
        if mode == "rolling":
            train_start = max(0, train_end - min_train)
        else:
            train_start = 0

        train_idx = np.arange(train_start, train_end, dtype=int)
        test_idx = np.arange(test_start, test_end, dtype=int)
        if len(train_idx) < min_train:
            logger.info("purged_walk_forward: fold %d skipped (train=%d < min_train=%d)",
                        k, len(train_idx), min_train)
            continue
        produced += 1
        yield train_idx, test_idx

    if produced == 0:
        logger.warning("purged_walk_forward produced no folds: n=%d, n_splits=%d, min_train=%d",
                       n, n_splits, min_train)
