"""Parity: local research/walk_forward.purged_walk_forward vs Blue_engine walk_forward.

Blue's splits.py is loaded alone (its only relative import is stubbed), so this runs in
the sandbox. Compares every fold's train and test indices on a grid of inputs.
"""
import importlib.util
import pathlib
import sys
import types

import numpy as np
import pytest

from quant.research.walk_forward import purged_walk_forward

SPLITS_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant" / "blue_engine" / "ml" / "evaluation" / "splits.py"


@pytest.fixture
def blue_walk_forward(monkeypatch):
    names = ["blue_engine", "blue_engine.ml", "blue_engine.ml.evaluation"]
    noop = lambda *a, **k: None  # noqa: E731
    logger = types.SimpleNamespace(info=noop, warning=noop, debug=noop)
    root = types.ModuleType("blue_engine"); root.__path__ = []
    ml = types.ModuleType("blue_engine.ml"); ml.logger = logger
    ev = types.ModuleType("blue_engine.ml.evaluation"); ev.__path__ = []
    for name, mod in zip(names, [root, ml, ev]):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("blue_engine.ml.evaluation.splits", SPLITS_SRC)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod.walk_forward


GRID = [
    # n, n_splits, purge, min_train, mode
    (100, 4, 5, 1, "expanding"),
    (103, 3, 4, 5, "expanding"),
    (100, 3, 1, 10, "rolling"),
    (250, 5, 5, 20, "expanding"),
    (250, 5, 5, 20, "rolling"),
    (60, 5, 3, 25, "expanding"),   # some folds skipped by min_train
    (9, 2, 0, 1, "expanding"),
    (7, 6, 2, 1, "expanding"),     # tiny: test_size floors to 1
]


@pytest.mark.parametrize("n,n_splits,purge,min_train,mode", GRID)
def test_folds_match_blue(blue_walk_forward, n, n_splits, purge, min_train, mode):
    blue = blue_walk_forward([{}] * n, n_splits=n_splits, horizon=purge, embargo=0,
                             mode=mode, min_train=min_train)
    local = list(purged_walk_forward(n, n_splits=n_splits, purge=purge,
                                     min_train=min_train, mode=mode))
    assert len(local) == len(blue)
    for (tr, te), split in zip(local, blue):
        assert np.array_equal(tr, np.asarray(split.train_idx, dtype=int))
        assert np.array_equal(te, np.asarray(split.test_idx, dtype=int))
