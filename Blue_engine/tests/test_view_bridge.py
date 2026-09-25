"""
tests/test_view_bridge.py — Phase 5: views into a constrained allocation.

The theme of these tests is **containment**. An ML forecaster is a model: it is
wrong regularly and it is most confident exactly when it is most wrong. So the
tests below assert the guard-rails, not the model quality:

* confidence is DISCOUNTED before it is capped (so 0.99 cannot become 0.35);
* total tilt is bounded, and scaling preserves the view RANKING;
* a halt from the risk layer suppresses views entirely — a model cannot talk the
  desk into trading through a drawdown;
* views are opt-in (off by default).

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_view_bridge.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import view_bridge as vb  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _view(assets, ret=0.02, conf=0.5, **kw):
    d = {"assets": assets, "return": ret, "confidence": conf,
         "source": kw.pop("source", "ml")}
    d.update(kw)
    return d


def _inputs(**over):
    d = {"universe": ["NIFTY", "IT", "PHARMA"],
         "cov": [[0.02, 0.004, 0.002],
                 [0.004, 0.03, 0.003],
                 [0.002, 0.003, 0.04]],
         "n_obs": 300}
    d.update(over)
    return d


# --------------------------------------------------------------------------- #
# 1. Confidence must be discounted BEFORE it is capped
# --------------------------------------------------------------------------- #
def test_stated_confidence_is_discounted_then_capped():
    """0.99 stated must not become 0.35 by capping alone.

    Order matters: capping first, then scaling, would let a model's self-reported
    0.99 become 0.35 x 0.5 = 0.175 — under-stating it. Discounting first and
    capping second gives min(0.99 x 0.5, 0.35) = 0.35. The *stated* value is kept
    for the dashboard so the desk can see the difference.
    """
    info = vb.validate_views([_view(["IT"], conf=0.99)],
                             universe=["IT", "PHARMA"])
    used = info["views"][0]["confidence"]
    stated = info["views"][0]["stated_confidence"]
    _check(abs(used - 0.35) < 1e-9, f"expected 0.35 after 0.5x then cap, got {used}")
    _check(abs(stated - 0.99) < 1e-9, "the stated confidence must be preserved")
    _check(used < stated,
           "the model's own confidence must never be taken at face value")


def test_moderate_confidence_is_halved_not_capped():
    info = vb.validate_views([_view(["IT"], conf=0.20)],
                             universe=["IT", "PHARMA"])
    _check(abs(info["views"][0]["confidence"] - 0.10) < 1e-9,
           f"0.20 x 0.5 = 0.10, got {info['views'][0]['confidence']}")


def test_confidence_scale_is_configurable():
    info = vb.validate_views([_view(["IT"], conf=0.40)],
                             universe=["IT"],
                             policy={"confidence_scale": 0.25,
                                     "max_confidence": 0.9})
    _check(abs(info["views"][0]["confidence"] - 0.10) < 1e-9,
           f"0.40 x 0.25 = 0.10, got {info['views'][0]['confidence']}")


def test_below_min_confidence_is_dropped():
    info = vb.validate_views([_view(["IT"], conf=0.01)], universe=["IT"],
                             policy={"confidence_scale": 0.5,
                                     "min_confidence": 0.05})
    _check(info["n_out"] == 0, "a 0.005 confidence view is noise, drop it")
    _check(any("min" in (d.get("reason") or "") for d in info["dropped"]),
           f"the drop should name the threshold: {info['dropped']}")


# --------------------------------------------------------------------------- #
# 2. Validation: junk must not reach the optimiser
# --------------------------------------------------------------------------- #
def test_view_for_an_unknown_sleeve_is_dropped():
    info = vb.validate_views([_view(["ZZZ_NOT_HELD"])],
                             universe=["NIFTY", "IT"])
    _check(info["n_out"] == 0, "views for sleeves we do not hold are dropped")
    _check(any("universe" in (d.get("reason") or "") for d in info["dropped"]),
           f"reason should mention the universe: {info['dropped']}")


def test_zero_return_view_is_dropped():
    info = vb.validate_views([_view(["IT"], ret=0.0)], universe=["IT"])
    _check(info["n_out"] == 0, "a zero-return view carries no information")


def test_malformed_view_is_dropped_not_fatal():
    info = vb.validate_views([_view(["IT"], ret=None), {}, _view(["IT"])],
                             universe=["IT"])
    _check(info["ok"] is True, "malformed views must not raise")
    _check(info["n_in"] == 3 and info["n_out"] == 1,
           f"only the valid one survives: {info['n_in']} -> {info['n_out']}")


def test_max_views_keeps_the_most_confident():
    """Correlated views compound, so many weak views can beat one strong one."""
    raw = [_view([f"S{i}"], conf=0.10 + i * 0.02) for i in range(8)]
    uni = [f"S{i}" for i in range(8)]
    info = vb.validate_views(raw, universe=uni,
                             policy={"max_views": 3, "confidence_scale": 1.0,
                                     "max_confidence": 1.0})
    _check(info["n_out"] == 3, f"cap at 3 views, got {info['n_out']}")
    confs = [v["confidence"] for v in info["views"]]
    _check(confs == sorted(confs, reverse=True),
           f"the most confident must survive: {confs}")
    _check(any("max_views" in (d.get("reason") or "") for d in info["dropped"]),
           "the drop reason should mention the cap")


def test_views_are_sorted_by_confidence():
    raw = [_view(["A"], conf=0.10), _view(["B"], conf=0.30)]
    info = vb.validate_views(raw, universe=["A", "B"],
                             policy={"confidence_scale": 1.0,
                                     "max_confidence": 1.0})
    _check(info["views"][0]["assets"] == ["B"],
           f"highest confidence first, got {info['views']}")


# --------------------------------------------------------------------------- #
# 3. Tilt cap must preserve the view RANKING
# --------------------------------------------------------------------------- #
def test_tilt_under_the_cap_is_untouched():
    out = vb.cap_tilt({"A": 0.5, "B": 0.5}, {"A": 0.55, "B": 0.45},
                      max_tilt=0.15)
    _check(out["capped"] is False, "a 0.05 tilt is inside a 0.15 cap")
    _check(abs(out["weights"]["A"] - 0.55) < 1e-9, "weights pass through")


def test_tilt_over_the_cap_is_scaled_not_clamped():
    """Scaling preserves the model's ranking; clamping would destroy it.

    Clamping the strongest sleeve to the cap would flatten the biggest view
    first and leave the book with arbitrary leftovers — worse than useless, since
    the desk would act on a ranking the model never produced.
    """
    out = vb.cap_tilt({"A": 0.5, "B": 0.5}, {"A": 0.95, "B": 0.05},
                      max_tilt=0.15)
    _check(out["capped"] is True, "a 0.45 tilt must be capped")
    _check(abs(out["tilt_after"] - 0.15) < 1e-6,
           f"tilt should land exactly on the cap, got {out['tilt_after']}")
    _check(out["weights"]["A"] > out["weights"]["B"],
           f"the original ranking must survive: {out['weights']}")
    _check(out["weights"]["A"] > 0.5,
           "A was tilted up and must stay above the prior")


def test_tilt_cap_handles_an_already_wide_prior():
    out = vb.cap_tilt({"A": 0.9, "B": 0.05, "C": 0.05},
                      {"A": 0.30, "B": 0.35, "C": 0.35}, max_tilt=0.10)
    _check(out["capped"] is True, "a 0.6 tilt must be capped")
    _check(abs(out["tilt_after"] - 0.10) < 1e-6, "must land on the cap")


def test_tilt_with_identical_books_is_a_noop():
    out = vb.cap_tilt({"A": 0.5, "B": 0.5}, {"A": 0.5, "B": 0.5})
    _check(out["capped"] is False and out["tilt_before"] == 0.0,
           "identical books have no tilt")


# --------------------------------------------------------------------------- #
# 4. THE containment rule: the risk layer outranks the model
# --------------------------------------------------------------------------- #
def test_views_are_suppressed_when_the_risk_layer_halts():
    """A model cannot talk the desk into trading through a drawdown.

    The Phase-4 decision is evaluated BEFORE the ML layer is consulted, so a
    halted book has no views attached at all — the model is not even asked.
    """
    decision = {"ok": True, "trade": False, "action": "EXIT_TO_CASH",
                "optimiser": None, "constraints": {}}
    inp = _inputs(views=[_view(["IT"], conf=0.9)])
    problem = vb.build_problem(inp, decision=decision, enabled=True)
    _check(problem.get("views") == [],
           f"a halted book must carry no views: {problem.get('views')}")
    info = problem.get("view_info") or {}
    _check(any("halted" in w for w in (info.get("warnings") or [])),
           f"suppression must be explained: {info}")


def test_views_flow_when_the_risk_layer_permits_trading():
    decision = {"ok": True, "trade": True, "action": "OPTIMISE",
                "optimiser": "black_litterman", "constraints": {}}
    inp = _inputs(views=[_view(["IT"], ret=0.03, conf=0.8)])
    problem = vb.build_problem(inp, decision=decision, enabled=True)
    _check(len(problem.get("views") or []) == 1,
           f"a permitted book should receive the view: {problem.get('views')}")
    _check(problem["views"][0]["confidence"] < 0.8,
           "the view must arrive discounted")


def test_views_are_opt_in_by_default():
    """A deployment that has not thought about ML risk gets no ML tilts."""
    inp = _inputs(views=[_view(["IT"], conf=0.9)])
    problem = vb.build_problem(inp, enabled=False)
    _check(problem.get("views") == [],
           f"must be off unless explicitly enabled: {problem.get('views')}")


def test_constraints_from_the_decision_are_attached():
    decision = {"trade": True, "optimiser": "hrp",
                "constraints": {"max_weight": 0.25, "max_gross": 0.8}}
    problem = vb.build_problem(_inputs(), decision=decision, enabled=False)
    _check(abs(problem["constraints"]["max_weight"] - 0.25) < 1e-9,
           "the decision's constraints must reach the problem")
    _check(problem["optimiser"] == "hrp", "the chosen optimiser must be attached")


# --------------------------------------------------------------------------- #
# 5. View summaries + degradation
# --------------------------------------------------------------------------- #
def test_summarise_shows_stated_versus_allowed():
    """The gap between stated and allowed confidence IS the risk layer working."""
    info = vb.validate_views([_view(["IT"], conf=0.95)], universe=["IT"])
    out = vb.summarise_views(info)
    row = out["views"][0]
    _check(row["stated_confidence"] == 0.95, "stated preserved")
    _check(row["used_confidence"] < row["stated_confidence"],
           "used must be lower than stated")
    _check(out["n_in"] == 1 and out["n_out"] == 1, "counts")


def test_summarise_handles_an_empty_summary():
    out = vb.summarise_views(None)
    _check(out["n_in"] == 0 and out["views"] == [],
           f"no info should summarise to empty, got {out}")


def test_build_problem_never_raises_on_garbage():
    for junk in (None, {}, {"universe": None}, {"universe": ["A"],
                                                "views": "not-a-list"}):
        out = vb.build_problem(junk)
        _check(isinstance(out, dict) and "universe" in out,
               f"every input must produce a problem: {junk}")


def test_collect_views_without_inputs_is_empty():
    _check(vb.views_from_inputs({}) == [], "no views supplied -> none collected")
    _check(vb.views_from_inputs(None) == [], "None is safe")


def test_collect_views_passes_through_explicit_views():
    inp = {"universe": ["IT"], "views": [_view(["IT"])]}
    out = vb.views_from_inputs(inp, enabled=True)
    _check(len(out) == 1, f"explicit views must pass through: {out}")


# --------------------------------------------------------------------------- #
# Runner (works without pytest)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:        # needs pytest fixtures
                print(f"SKIP {name} (needs pytest fixtures)")
                continue
            fn()
            print(f"PASS {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())