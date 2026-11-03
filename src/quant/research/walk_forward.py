"""Walk-forward splits with a purge gap before each test block.

Flow stage: Walk-forward + purged CV
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/ml/evaluation/walk_forward.py
"""
from __future__ import annotations

from typing import Iterator, Tuple

import numpy as np


def purged_walk_forward(
    n: int, n_splits: int, test_size: int, purge: int = 0, min_train: int = 1
) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    """Expanding-window folds at the end of the sample. Train stops `purge` bars before each test.

    Embargo is not needed for expanding windows: no training bar comes after a test bar.
    """
    if n_splits * test_size >= n:
        raise ValueError("n_splits * test_size must be less than n")
    for k in range(n_splits):
        ts = n - (n_splits - k) * test_size
        train = np.arange(0, max(0, ts - purge))
        if len(train) < min_train:
            continue
        yield train, np.arange(ts, ts + test_size)
