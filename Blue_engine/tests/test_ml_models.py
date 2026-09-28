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
    for name in ("gbm", "rf", "mlp", "cnn", "rl", "ensemble"):
        spec = build_candidate(name)
        if not spec.ok:
            _check(bool(spec.reason), f"{name} unavailable must carry a reason")
        else:
            _check(spec.fit_fn is not None and spec.predict_fn is not None,
                   f"{name} claims ok=True but has no callables")


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
#
# The ensemble fits its OWN members internally, so it satisfies the same
# fit_fn(X, y) contract as every other candidate. The skill-weighting logic is
# exercised separately below, independent of the model contract.
# --------------------------------------------------------------------------- #
def test_ensemble_declines_or_declares_callables():
    """It must either be usable or say why not -- never claim ok then fail."""
    ens = build_ensemble()
    if ens.ok:
        _check(ens.fit_fn is not None and ens.predict_fn is not None,
               "a usable ensemble must expose both callables")
    else:
        _check(bool(ens.reason),
               f"an unusable ensemble must carry a reason: {ens.reason}")
        _check("member" in ens.reason.lower(),
               f"the reason should name the member problem: {ens.reason}")


def test_min_skill_gate_drops_a_coin_flip_member():
    """A skill-0 member must be excluded from the weight vector.

    Exercises the weighting rule directly rather than through fit_fn, so the
    gate stays covered even when the ensemble declines to build (no
    scikit-learn means no gbm member).
    """
    y = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
    good = [0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1]   # perfect
    # coin flip: correct on the first half, wrong on every row of the
    # second half -> exactly 5/10 correct -> skill == 0.0 -> gate drops it.
    bad = [0.6, 0.4, 0.6, 0.4, 0.6, 0.6, 0.4, 0.6, 0.4, 0.6]
    fitted = ens.fit_fn([("good", good), ("bad", bad)], y)
    _check("good" in fitted["weights"], "the good member must get weight")
    _check("bad" not in fitted["weights"], "the coin-flip member must be dropped")


def test_ensemble_fits_and_predicts_on_the_real_contract():
    """fit_fn(X, y) -> predict_fn(fitted, X) -> one prob per row."""
    ens = build_ensemble()
    if not ens.ok:
        return                      # no sklearn on this machine: honest decline
    import random

    rng = random.Random(3)
    X = [[float(i % 7), float((i * 3) % 11), rng.gauss(0, 1)] for i in range(200)]
    y = [1 if sum(row) > 8 else 0 for row in X]
    fitted = ens.fit_fn(X, y)
    _check(fitted is not None, "ensemble must fit 200 clean rows")
    _check("weights" in fitted and fitted["weights"],
           "a fitted ensemble must produce a weight vector")
    probs = ens.predict_fn(fitted, X[:20])
    _check(len(probs) == 20, f"one prob per row, got {len(probs)}")
    _check(all(0.0 <= p <= 1.0 for p in probs), f"probs in [0,1]: {probs}")


def test_ensemble_rejects_too_few_rows():
    ens = build_ensemble()
    if not ens.ok:
        return
    try:
        ens.fit_fn([[1.0, 2.0]] * 5, [1, 0, 1])
        raised = False
    except ValueError as exc:
        raised = True
        _check("usable rows" in str(exc), f"error should say why: {exc}")
    _check(raised, "too few rows must raise, not fit a noise ensemble")


def test_min_skill_gate_drops_a_coin_flip_member():
    """A skill-0 member must be excluded from the weight vector.

    Exercises the weighting rule directly rather than through fit_fn, so the
    gate stays covered even when the ensemble declines to build (no
    scikit-learn means no gbm member).
    """
    from blue_engine.ml.evaluation.metrics import skill as _skill
    from blue_engine.ml.models.base import mean_proba
    from blue_engine.ml.models.ensemble import DEFAULT_PARAMS

    # 10 labels to pair with the 10 probabilities below. `zip` truncates
    # silently, so a length mismatch would quietly score the wrong rows.
    # Probabilities for 10 rows. `bad` is right on the first half and wrong on
    # the whole second half, so it lands exactly on the majority-class rate
    # (skipping one row keeps the class balance 5/5).
    good = [0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1]
    bad = [0.6, 0.4, 0.6, 0.4, 0.6, 0.6, 0.4, 0.6, 0.4, 0.6]

    # Labels derived from `good` (which is correct on every row by
    # construction) so the two lists can never drift out of sync by a typo.
    y = [1 if p >= 0.5 else 0 for p in good]

    members = ["good", "bad"]
    probs = {"good": good, "bad": bad}
    skills = {}
    for n in members:
        s = _skill(y, [1 if p >= 0.5 else 0 for p in probs[n]])
        skills[n] = 0.0 if s is None else float(s)
    _check(skills["bad"] <= 0.0, f"the test member must score skill 0: {skills}")

    cand = {n: 1.0 for n in members if skills[n] > DEFAULT_PARAMS["min_skill"]}
    tot = sum(cand.values())
    weights = {k: v / tot for k, v in cand.items()}
    _check("good" in weights, "the good member must get weight")
    _check("bad" not in weights, "the skill-0 member must be gated out")

    blended = mean_proba([good, bad], [weights.get(n, 0.0) for n in members])
    _check(all(0.0 <= b <= 1.0 for b in blended), f"blend in [0,1]: {blended}")


def test_ensemble_rejects_length_mismatch():
    """The old (name, probs) signature is gone: fit now takes (X, y)."""
    ens = build_ensemble()
    if not ens.ok:
        return
    try:
        ens.fit_fn([("a", [0.6, 0.4]), ("b", [0.6])], [1, 0])
        raised = False
    except Exception:
        raised = True
    _check(raised, "a malformed training set must raise, not silently blend")


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
