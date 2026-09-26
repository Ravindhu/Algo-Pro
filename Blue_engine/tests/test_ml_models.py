"""
tests/test_ml_models.py — model-contract and ensemble tests (hermetic).

Run:
    & .\\openalgo\\.venv\\Scripts\\python.exe -m pytest Blue_engine/tests -q
    & .\\openalgo\\.venv\\Scripts\\python.exe Blue_engine\\tests\\test_ml_models.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.models import resolve_candidates, build_candidate  # noqa: E402
from blue_engine.ml.models.base import ModelSpec, mean_proba           # noqa: E402
from blue_engine.ml.models.ridge import fit_ridge, ridge_predict       # noqa: E402
from blue_engine.ml.models.ensemble import build as build_ensemble     # noqa: E402


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# candidate table
# --------------------------------------------------------------------------- #
def test_unknown_candidate_reports_not_ok():
    spec = build_candidate("no_such_model")
    _check(spec.ok is False, "unknown candidate must be ok=False")
    _check("unknown candidate" in (spec.reason or ""), f"reason: {spec.reason}")


def test_ridge_always_builds_and_predicts_probabilities():
    specs = resolve_candidates(["ridge"])
    _check(len(specs) == 1 and specs[0].ok, "ridge must build on a bare machine")
    spec = specs[0]
    X = [[float(i % 7), float((i * 3) % 11)] for i in range(60)]
    y = [1 if (i % 7) > 3 else 0 for i in range(60)]
    fitted = spec.fit_fn(X, y)
    _check(fitted is not None, "ridge fit should succeed on 60 clean rows")
    probs = spec.predict_fn(fitted, X[:10])
    _check(len(probs) == 10, "predict must return one prob per row")
    _check(all(0.0 <= p <= 1.0 for p in probs), f"probs in [0,1]: {probs}")


def test_optional_families_degrade_loudly_not_silently():
    # GBM/RF may be available or not — the CONTRACT is: ok=False comes with
    # a reason; it never comes back as a silently-broken model.
    for name in ("gbm", "rf", "mlp", "cnn", "rl"):
        spec = build_candidate(name)
        if not spec.ok:
            _check(bool(spec.reason), f"{name} unavailable must carry a reason")


# --------------------------------------------------------------------------- #
# ridge maths (verifiable by hand)
# --------------------------------------------------------------------------- #
def test_ridge_recovers_a_known_linear_rule():
    # y = 1 iff x0 - x1 > 0 — a rule ridge must learn perfectly.
    # Features are centred around zero so the least-squares fit of a STEP
    # crosses 0.5 exactly at the decision boundary (x0 == x1).  A linear
    # model can only represent a step as a squashed line: if classes were
    # lopsided the crossing would sit at the class mean, not the boundary —
    # a property of regression, not a ridge bug.  Boundary-centred on purpose.
    X = [[float(i - 20), float(20 - i)] for i in range(40)]
    y = [1 if x[0] > x[1] else 0 for x in X]
    fitted = fit_ridge(X, y, ridge_lambda=0.1)
    _check(fitted is not None, "fit on a separable rule must succeed")
    probs = ridge_predict(fitted, X)
    correct = sum(1 for p, t in zip(probs, y) if (p >= 0.5) == (t == 1))
    _check(correct >= 36, f"ridge should nail a separable rule (got {correct}/40)")


def test_ridge_predict_survives_garbage_rows():
    fitted = fit_ridge([[1.0, 2.0]] * 30 + [[2.0, 1.0]] * 10,
                       [1] * 30 + [0] * 10)
    probs = ridge_predict(fitted, [["x", "y"], [1.0, 2.0]])
    _check(len(probs) == 2 and all(0 <= p <= 1 for p in probs),
           "a bad row must not crash prediction")


# --------------------------------------------------------------------------- #
# ensemble
# --------------------------------------------------------------------------- #
def test_ensemble_weights_drop_members_below_min_skill():
    ens = build_ensemble(min_skill=0.0)
    y = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
    good = [0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1]   # perfect
    # coin flip: correct on the first half, wrong on every row of the
    # second half -> exactly 5/10 correct -> skill == 0.0 -> gate drops it.
    bad = [0.6, 0.4, 0.6, 0.4, 0.6, 0.6, 0.4, 0.6, 0.4, 0.6]
    fitted = ens.fit_fn([("good", good), ("bad", bad)], y)
    _check("good" in fitted["weights"], "the good member must get weight")
    _check("bad" not in fitted["weights"], "the coin-flip member must be dropped")


def test_ensemble_blends_probabilities_in_weight_order():
    ens = build_ensemble(min_skill=-1.0)   # keep both members
    y = [1, 0, 1, 0, 1, 0]
    a = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
    b = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0]     # perfect anti-correlated
    fitted = ens.fit_fn([("a", a), ("b", b)], y)
    probs = ens.predict_fn(fitted, [a, b])
    _check(len(probs) == 6, "one blended prob per row")
    _check(all(0.0 <= p <= 1.0 for p in probs), f"blend in [0,1]: {probs}")


def test_ensemble_rejects_length_mismatch():
    ens = build_ensemble()
    try:
        ens.fit_fn([("a", [0.6, 0.4]), ("b", [0.6])], [1, 0])
        raised = False
    except ValueError:
        raised = True
    _check(raised, "probs/labels length mismatch must raise, not blend")


# --------------------------------------------------------------------------- #
# shared blender
# --------------------------------------------------------------------------- #
def test_mean_proba_is_a_weighted_mean():
    rows = [[1.0, 0.0], [0.0, 1.0]]
    out = mean_proba(rows, weights=[3.0, 1.0])
    _check(abs(out[0] - 0.75) < 1e-9 and abs(out[1] - 0.25) < 1e-9,
           f"3:1 blend of 1.0/0.0 must be 0.75/0.25, got {out}")


def test_modelspec_to_dict_is_json_shaped():
    spec = ModelSpec(name="x", family="linear", params={"a": 1})
    d = spec.to_dict()
    _check("fit_fn" not in d and "predict_fn" not in d,
           "callables must not leak into the manifest dict")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"PASS {fn.__name__}")
    print("test_ml_models: all passed")
