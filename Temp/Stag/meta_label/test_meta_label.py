"""Meta-labeler tests, moved to staging with the module (2026-11-26).

The module lives at Temp/Stag/meta_label/meta_label.py and is NOT wired into the bot.
It is loaded from this folder, so these tests keep running here without the main tree.
Run: python -m pytest Temp/Stag/meta_label -q
"""
import importlib.util
import pathlib

import numpy as np
import pandas as pd
import pytest

from quant.models.calibration import CalibrationError, fit_logistic, predict_proba

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("meta_label_staged", _HERE / "meta_label.py")
_mod = importlib.util.module_from_spec(_spec)
import sys as _sys
_sys.modules[_spec.name] = _mod  # dataclasses needs the module registered
_spec.loader.exec_module(_mod)
MetaLabeler = _mod.MetaLabeler
MetaLabelError = _mod.MetaLabelError


def test_meta_labeler_skips_low_confidence_and_caps_bet():
    rng = np.random.default_rng(5)
    X = rng.normal(0, 1, (400, 2))
    y = (X[:, 0] > 0).astype(float)
    ml = MetaLabeler.fit(X, y, take_threshold=0.6, max_bet=0.05)
    take_hi, bet_hi = ml.decide([3.0, 0.0])
    take_lo, bet_lo = ml.decide([-3.0, 0.0])
    assert take_hi and bet_hi <= 0.05
    assert not take_lo and bet_lo == 0.0


def _labeler(**over):
    base = dict(w=np.array([1.0]), b=0.0)
    base.update(over)
    return MetaLabeler(**base)


def test_negative_max_bet_is_refused():
    """It used to return a NEGATIVE bet, which reverses the trade."""
    with pytest.raises(MetaLabelError, match="max_bet"):
        _labeler(max_bet=-0.5)


def test_zero_max_bet_is_refused():
    with pytest.raises(MetaLabelError, match="max_bet"):
        _labeler(max_bet=0.0)


def test_threshold_at_or_above_one_is_refused():
    """A threshold of 1 would silently skip every signal forever."""
    with pytest.raises(MetaLabelError, match="take_threshold"):
        _labeler(take_threshold=1.5)


def test_nan_feature_raises_instead_of_silently_skipping():
    with pytest.raises(MetaLabelError):
        _labeler().decide([float("nan")])


def test_infinite_feature_no_longer_returns_the_max_bet():
    """sigmoid(inf)=1 used to saturate to the LARGEST possible position."""
    with pytest.raises(MetaLabelError):
        _labeler().decide([float("inf")])


def test_fit_on_single_class_is_refused():
    with pytest.raises(CalibrationError):
        MetaLabeler.fit([[1.0], [2.0]], [1.0, 1.0])


def test_normal_path_is_unchanged():
    take, bet = _labeler().decide([100.0])
    assert take is True
    assert bet == pytest.approx(0.10)      # capped by max_bet


def test_bet_is_zero_when_not_taken():
    take, bet = _labeler(w=np.array([-1.0])).decide([10.0])
    assert take is False and bet == 0.0


def test_bet_never_exceeds_the_cap():
    for x in (50.0, 100.0, 500.0):
        _, bet = _labeler(max_bet=0.05).decide([x])
        assert 0.0 <= bet <= 0.05


def test_kelly_form_at_half_probability_is_zero():
    """f = 2p-1 is 0 at p=0.5, so an edge-less signal cannot be sized."""
    m = _labeler(w=np.array([0.0]), b=0.0, take_threshold=0.4)
    take, bet = m.decide([0.0])
    assert take is True and bet == 0.0


# --------------------------------------------------------------------------- #
# conviction: verify the existing fail-closed rules still hold
# --------------------------------------------------------------------------- #
