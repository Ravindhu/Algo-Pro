"""Leakage gate and walk-forward evaluation: the two research verdict layers.

Both wrap an engine module and both must fail closed. These tests cover the pass
path, every block path, and the honest "cannot certify" state, because a research
gate that reports a silent pass is worse than having no gate.
"""
import pytest

from quant.research import leakage_gate as lg
from quant.research import walk_forward_eval as wf


# --------------------------------------------------------------------------- #
# fixture data
# --------------------------------------------------------------------------- #
def _rows(n: int = 60):
    """n dated rows, oldest first, unique timestamps (yyyy-mm-dd)."""
    out = []
    for i in range(n):
        month, day = 1 + i // 28, 1 + i % 28
        out.append({"as_of_ts": f"2026-{month:02d}-{day:02d}", "x": float(i)})
    return out


def _lagged(rows):
    """A genuinely leak-free feature: yesterday's value, no future knowledge."""
    xs = [r["x"] for r in rows]
    return {"lag1": [None] + xs[:-1]}


# --------------------------------------------------------------------------- #
# leakage gate
# --------------------------------------------------------------------------- #
def test_clean_dataset_with_lagged_feature_is_clean():
    report = lg.audit(_rows(), build_features=_lagged)
    assert report["verdict"] == lg.CLEAN, report["detail"]
    assert report["checks_missing"] == []


def test_missing_timestamp_is_leaked():
    report = lg.audit([{"x": 1.0}] * 60, build_features=_lagged)
    assert report["verdict"] == lg.LEAKED
    assert report["fatal"] > 0


def test_empty_dataset_is_leaked_not_clean():
    """An empty dataset must never read as clean: nothing was validated."""
    report = lg.audit([])
    assert report["verdict"] == lg.LEAKED


def test_skipped_shift_test_is_incomplete_not_clean():
    """No build_features means the behavioural test did not run. Not a pass."""
    report = lg.audit(_rows())
    assert report["verdict"] == lg.INCOMPLETE
    assert "shift" in report["checks_missing"]


def test_full_sample_normalisation_is_caught_as_leak():
    """The classic killer: standardising over the whole series includes the future."""
    def leaky(rows):
        xs = [r["x"] for r in rows]
        mean = sum(xs) / len(xs)          # uses every row, including later ones
        return {"z": [(x - mean) for x in xs]}

    report = lg.audit(_rows(), build_features=leaky)
    assert report["verdict"] == lg.LEAKED
    assert "z" in report["leaked_features"]


def test_require_clean_returns_on_clean():
    report = lg.audit(_rows(), build_features=_lagged)
    assert lg.require_clean(report) is report


@pytest.mark.parametrize("rows,kwargs", [
    ([], {}),
    (_rows(), {}),                                   # incomplete: shift skipped
    ([{"x": 1.0}] * 60, {"build_features": _lagged}),  # leaked
])
def test_require_clean_refuses_everything_else(rows, kwargs):
    report = lg.audit(rows, **kwargs)
    with pytest.raises(ValueError, match="leakage gate refused"):
        lg.require_clean(report)


# --------------------------------------------------------------------------- #
# walk-forward evaluation
# --------------------------------------------------------------------------- #
def _wf(n=60, *, fit=None, predict=None, **kw):
    rows = [{"as_of_ts": f"d{i}"} for i in range(n)]
    X = [[float(i)] for i in range(n)]
    y = [1] * n
    fwd = [0.001] * n
    return wf.evaluate(
        rows, X, y,
        fit_fn=fit or (lambda Xtr, ytr: None),
        predict_fn=predict or (lambda m, Xte: [0.6] * len(Xte)),
        forward_returns=fwd,
        **kw,
    )


def test_dataset_too_short_is_inconclusive():
    r = _wf(40, n_splits=3, min_train=10)
    assert r["verdict"] in (wf.INCONCLUSIVE, wf.FAILED)
    assert not wf.is_significant(r)


def test_flat_predictions_are_not_significant():
    """No edge: a constant probability stream cannot beat random trials."""
    r = _wf(120, n_splits=3, min_train=40)
    assert not wf.is_significant(r)


def test_require_passed_refuses_non_significant():
    r = _wf(40, n_splits=3, min_train=10)
    with pytest.raises(ValueError, match="walk-forward refused"):
        wf.require_passed(r)


def test_is_significant_is_exact():
    """Only the PASSED verdict counts. Anything else is not evidence."""
    assert wf.is_significant({"verdict": wf.PASSED})
    for v in (wf.FAILED, wf.INCONCLUSIVE, wf.UNAVAILABLE, "", "ok", "pass"):
        assert not wf.is_significant({"verdict": v})


def test_evaluate_returns_engine_report():
    r = _wf(120, n_splits=3, min_train=40)
    assert set(r) >= {"verdict", "detail", "engine"}
    assert isinstance(r["engine"], dict)
    assert "verdict" in r
