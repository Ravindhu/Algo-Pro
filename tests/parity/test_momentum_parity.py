"""Parity: local models/factors.momentum vs Blue_engine ml/features/engineering.momentum.

Blue's engineering.py is loaded alone (its only relative import is stubbed), so this runs
in the sandbox. Compares NaN pattern and values on random and edge-case series.
"""
import importlib.util
import pathlib
import sys
import types

import numpy as np
import pandas as pd
import pytest

from quant.models.factors import momentum

ENG_SRC = pathlib.Path(__file__).resolve().parents[2] / "Blue_engine" / "ml" / "features" / "engineering.py"


@pytest.fixture
def blue_momentum(monkeypatch):
    noop = lambda *a, **k: None  # noqa: E731
    logger = types.SimpleNamespace(info=noop, warning=noop, debug=noop)
    root = types.ModuleType("blue_engine"); root.__path__ = []
    root.logger = logger
    ml = types.ModuleType("blue_engine.ml"); ml.__path__ = []; ml.logger = logger
    feats = types.ModuleType("blue_engine.ml.features")
    for name, mod in [("blue_engine", root), ("blue_engine.ml", ml), ("blue_engine.ml.features", feats)]:
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("blue_engine.ml.features.engineering", ENG_SRC)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod.momentum


def _compare(blue, closes, window, shift):
    vals = blue(list(closes), window, shift=shift)
    local = momentum(pd.DataFrame({"x": closes}), window, shift=shift)["x"].to_numpy()
    expected = np.array([np.nan if v is None else v for v in vals], dtype=float)
    assert np.array_equal(np.isnan(local), np.isnan(expected))
    ok = ~np.isnan(expected)
    assert np.allclose(local[ok], expected[ok], rtol=1e-12, atol=0.0)


@pytest.mark.parametrize("window,shift", [(231, 21), (10, 1), (5, 0), (3, 2)])
def test_random_series_match_blue(blue_momentum, window, shift):
    rng = np.random.default_rng(7)
    closes = list(100 * np.cumprod(1 + rng.normal(0, 0.01, 300)))
    _compare(blue_momentum, closes, window, shift)


def test_missing_and_zero_base_are_nan_like_blue(blue_momentum):
    closes = [10.0, None, 12.0, 0.0, 14.0, 15.0, None, 16.0, 17.0, 18.0]
    _compare(blue_momentum, closes, 2, 1)


def test_negative_shift_refused():
    with pytest.raises(ValueError):
        momentum(pd.DataFrame({"x": [1.0, 2.0]}), 1, shift=-1)
