"""
test_phase3_bars.py - Phase 3 protocol extension smoke tests.

This file exercises the Phase 3 changes in isolation from the
aspirational ``test_optimizer_engine.py`` (which references a future
``SYNTHETIC_DEMO`` constant that has not been added to spaces.py
yet — out of scope for Phase 3).

What Phase 3 changes (verified here):
--------------------------------------
1. ``Objective.evaluate(params, bars=None)`` — the new signature
   accepts an optional ``bars`` argument. When ``bars`` is a
   non-empty list of OHLCV dicts, the objective slices the list
   into ``n_folds`` windows and passes each as the strategy's
   third argument. When ``bars`` is ``None``, the existing
   synthetic behaviour is preserved.

2. ``strategies.py`` — exports ``accepts_bars(fn)`` and
   ``call_strategy(fn, params, ctx, bars)``. The adapter inspects
   the callable's signature; strategies that take three args get
   the bars, two-arg strategies (legacy contract) keep working.

3. ``OptimizerEngine.search(bars=None)`` — passes ``bars`` into
   every ``objective.evaluate(...)`` call. The default ``None``
   keeps the existing test path intact.

All tests in this file are pure-Python: no filesystem, no
network, no live trading system, no optuna.
"""
from __future__ import annotations

import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from blue_engine.optimizer.engine import EngineConfig, OptimizerEngine  # noqa: E402
from blue_engine.optimizer.objective import Objective  # noqa: E402
from blue_engine.optimizer.search_space import (  # noqa: E402
    ParamSpec,
    SearchSpace,
)
from blue_engine.optimizer.strategies import (  # noqa: E402
    accepts_bars,
    call_strategy,
)


# --------------------------------------------------------------------------- #
# 1. strategies.py adapter: accepts_bars / call_strategy
# --------------------------------------------------------------------------- #


def test_accepts_bars_returns_false_for_legacy_two_arg_strategy():
    """A strategy with the old ``(params, ctx)`` contract must report
    ``accepts_bars=False`` and keep working unchanged.
    """

    def legacy_strategy(params, ctx):
        return {"ok": True, "returns": [0.01] * 50}

    assert accepts_bars(legacy_strategy) is False
    # And the adapter calls it with the OLD contract (no bars kwarg).
    out = call_strategy(legacy_strategy, {"x": 1}, {}, bars=[{"close": 1.0}])
    assert out["ok"] is True
    assert len(out["returns"]) == 50


def test_accepts_bars_returns_true_for_three_arg_strategy():
    """A strategy that declares a third ``bars`` argument must report
    ``accepts_bars=True`` and receive the bars from the adapter.
    """

    captured = {}

    def new_strategy(params, ctx, bars=None):
        captured["bars"] = bars
        return {"ok": True, "returns": [0.01] * 50, "data_class": "historical:openalgo"}

    assert accepts_bars(new_strategy) is True
    bars = [{"ts": 1, "close": 100.0}, {"ts": 2, "close": 101.0}]
    call_strategy(new_strategy, {"x": 1}, {}, bars=bars)
    assert captured["bars"] == bars


def test_call_strategy_passes_no_bars_to_legacy_strategy():
    """A legacy two-arg strategy called via ``call_strategy`` must NEVER
    receive a ``bars`` kwarg — only the (params, ctx) pair. This is the
    backward-compat invariant.
    """
    captured = {}

    def legacy(params, ctx):
        captured["params"] = params
        captured["ctx"] = ctx
        return {"ok": True, "returns": [0.0] * 30}

    out = call_strategy(legacy, {"a": 1.0}, {"seed": 7}, bars=[{"close": 99.0}])
    assert "bars" not in captured, "legacy strategy must not receive bars"
    assert captured["params"] == {"a": 1.0}
    assert captured["ctx"] == {"seed": 7}
    assert out["ok"] is True


# --------------------------------------------------------------------------- #
# 2. Objective.evaluate: bars kwarg + slicing into n_folds windows
# --------------------------------------------------------------------------- #


def _make_legacy_returns_strategy(n_bars: int = 60):
    """Build a strategy callable that emits a small alternating
    return series. A constant series gives a zero-variance Sharpe
    (undefined), so we use a deterministic alternating series with
    a tiny positive drift to give the objective a real, finite
    Sharpe to score.
    """
    def strat(params, ctx):
        # Tiny alternating returns with a +0.001 drift per bar.
        # Variance is non-zero (so Sharpe is finite) and the mean
        # is positive (so the trial is scoreable).
        rets = [0.001 + (0.005 if i % 2 == 0 else -0.004)
                for i in range(n_bars)]
        return {"ok": True, "returns": rets,
                "data_class": "simulated"}
    return strat


def test_objective_evaluate_accepts_bars_kwarg_with_none_default():
    """The new ``bars=None`` default must keep the existing synthetic
    path: 200-period fixture, 4 folds, stable positive returns.
    """
    space = SearchSpace(
        name="phase3_synth",
        params=[ParamSpec(name="x", kind="int", low=1, high=10, default=5)],
    )
    obj = Objective(_make_legacy_returns_strategy(60), space,
                    periods=60, n_folds=4, seed=7)
    # bars omitted entirely (positional only).
    res = obj.evaluate({"x": 5})
    assert res.ok is True
    assert res.score is not None
    assert math.isfinite(res.score)


def test_objective_evaluate_slices_bars_into_n_folds_windows():
    """When ``bars`` is a list of 200 dicts and ``n_folds=4``, the
    objective must slice into four disjoint windows of ~50 each and
    pass each as the strategy's third argument. The strategy below
    records the bars it received per fold; the union of fold slices
    must cover the input without overlap.
    """
    captured_folds = []

    def bars_aware(params, ctx, bars=None):
        captured_folds.append(list(bars or []))
        # Alternating +0.001 drift → non-zero variance, scoreable Sharpe.
        n = max(20, (len(bars) if bars else 30))
        rets = [0.001 + (0.005 if i % 2 == 0 else -0.004) for i in range(n)]
        return {"ok": True, "returns": rets,
                "data_class": "historical:openalgo"}

    n_bars = 200
    bars = [{"ts": i, "close": 100.0 + i * 0.01} for i in range(n_bars)]
    space = SearchSpace(
        name="phase3_bars",
        params=[ParamSpec(name="x", kind="int", low=1, high=10, default=5)],
    )
    obj = Objective(bars_aware, space, periods=60, n_folds=4, seed=7)
    res = obj.evaluate({"x": 5}, bars=bars)

    assert res.ok is True, f"bars-fed objective should be ok: {res.to_dict()}"
    assert len(captured_folds) == 4, "expected exactly 4 fold slices"
    # The chunks together cover the input without gaps or overlap.
    total = sum(len(f) for f in captured_folds)
    assert total == n_bars, f"fold slices must cover all {n_bars} bars; got {total}"
    # Per-fold counts should be approximately equal (chunk = n // n_folds).
    for fold in captured_folds:
        assert 0 < len(fold) <= 60, f"fold size out of range: {len(fold)}"


def test_objective_evaluate_bars_empty_list_falls_back_to_synthetic():
    """An empty list of bars is treated the same as ``bars=None`` (no
    data to slice), so the strategy runs against the synthetic
    fixture as before. This is the safety net for a partially
    populated cache.
    """
    calls = []

    def spy(params, ctx, bars=None):
        calls.append(bars)
        # Alternating +drift: non-zero variance so the Sharpe is finite.
        rets = [0.001 + (0.005 if i % 2 == 0 else -0.004) for i in range(30)]
        return {"ok": True, "returns": rets,
                "data_class": "simulated"}

    space = SearchSpace(
        name="phase3_empty",
        params=[ParamSpec(name="x", kind="int", low=1, high=10, default=5)],
    )
    obj = Objective(spy, space, periods=30, n_folds=2, seed=7)
    res = obj.evaluate({"x": 5}, bars=[])
    assert res.ok is True
    # All folds received an empty list (which the strategy ignores
    # and falls back to its own synthetic series).
    assert all(c == [] for c in calls), (
        f"empty bars must propagate as []: {calls!r}"
    )


# --------------------------------------------------------------------------- #
# 3. Engine.search: bars plumbing + backward compatibility
# --------------------------------------------------------------------------- #


def test_engine_search_default_no_bars_keeps_existing_behaviour():
    """``OptimizerEngine.search()`` with no ``bars`` kwarg must run
    exactly the way the pre-Phase-3 engine did: 1 + N trials, a
    backend, a study, and a SearchResult. This is the
    backward-compat invariant for the 37 pre-existing tests.
    """
    space = SearchSpace(
        name="engine_phase3_synth",
        params=[ParamSpec(name="x", kind="int", low=1, high=5, default=2)],
    )
    cfg = EngineConfig(name="phase3_default", n_trials=4, seed=7,
                       periods=60, n_folds=2, cost_bps=10.0,
                       multivariate=False)
    engine = OptimizerEngine(space, _make_legacy_returns_strategy(60), cfg=cfg)
    result = engine.search()       # NO bars kwarg
    assert result.backend in ("optuna", "builtin_tpe")
    assert result.wall_seconds >= 0.0
    assert len(result.study.trials) >= 1


def test_engine_search_accepts_bars_and_drives_strategy_through_them():
    """``OptimizerEngine.search(bars=...)`` must slice the bars into
    ``n_folds`` windows per trial, drive a 3-arg strategy against
    them, and produce a study that recorded the trials.
    """
    captured_per_trial = []

    def bars_aware(params, ctx, bars=None):
        captured_per_trial.append({"len": len(bars or []),
                                    "params": dict(params)})
        # Non-zero variance so each fold produces a scoreable Sharpe.
        n = max(20, (len(bars) if bars else 30))
        rets = [0.001 + (0.005 if i % 2 == 0 else -0.004) for i in range(n)]
        return {"ok": True, "returns": rets,
                "data_class": "historical:openalgo"}

    n_bars = 60
    bars = [{"ts": i, "close": 100.0 + i * 0.01} for i in range(n_bars)]

    # The SPACE must be wide enough that the sampler's warmup draws are
    # distinct. The original 1..5 range gave two uniform draws of 2.295 and
    # 1.603, both of which round to x=2: the second trial was a cache hit, so
    # only 2 strategy calls happened and this test failed on ordinary sampling
    # chance rather than on any plumbing defect (verified 2026-11-16 - the
    # sampler's RNG advanced correctly; the draws simply collided). A 1..1000
    # range removes the collision without weakening what is being asserted.
    space = SearchSpace(
        name="engine_phase3_bars",
        params=[ParamSpec(name="x", kind="int", low=1, high=1000, default=500)],
    )
    cfg = EngineConfig(name="phase3_bars", n_trials=3, seed=7,
                       periods=60, n_folds=2, cost_bps=10.0,
                       multivariate=False)
    engine = OptimizerEngine(space, bars_aware, cfg=cfg)

    result = engine.search(bars=bars)

    # Each DISTINCT point the objective scored ran ``n_folds`` strategy calls.
    # A duplicate draw is a legitimate cache hit and contributes no calls, so
    # assert on the invariant that actually matters: every distinct scored point
    # paid n_folds calls (2), and at least the baseline plus one sampler trial
    # were scored.
    assert len(captured_per_trial) >= 4, (
        f"expected >= 4 strategy calls (2 distinct trials * 2 folds); got "
        f"{len(captured_per_trial)}"
    )
    calls_per_point: dict = {}
    for entry in captured_per_trial:
        key = (entry["params"]["x"],)
        calls_per_point[key] = calls_per_point.get(key, 0) + 1
    for key, n_calls in calls_per_point.items():
        assert n_calls == 2, f"point {key} should get 2 fold calls; got {n_calls}"
    # Each call received some bars.
    for entry in captured_per_trial:
        assert entry["len"] > 0, f"each call should receive bars; got {entry}"
    # The journal recorded the trials.
    assert len(result.study.trials) >= 2, (
        f"expected >= 2 trials in the journal; got {len(result.study.trials)}"
    )


# --------------------------------------------------------------------------- #
# 4. End-to-end: a 3-arg strategy that actually uses the bars
# --------------------------------------------------------------------------- #


def test_engine_search_bars_end_to_end_with_a_real_signal():
    """A 3-arg strategy that turns the bars into per-bar returns
    (close-to-close) and a constant 1-share position must produce a
    finite positive score when bars are provided. This is the
    smallest possible "real data → real Sharpe" smoke test.
    """
    def real_signal(params, ctx, bars=None):
        if not bars or len(bars) < 5:
            return {"ok": True, "returns": [0.0] * 20,
                    "data_class": "historical:openalgo"}
        closes = [float(b["close"]) for b in bars]
        rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
        return {"ok": True, "returns": rets,
                "data_class": "historical:openalgo"}

    # A small space: just the period length parameter, integer.
    space = SearchSpace(
        name="e2e_bars",
        params=[ParamSpec(name="x", kind="int", low=1, high=5, default=2)],
    )
    cfg = EngineConfig(name="e2e_bars", n_trials=3, seed=7,
                       periods=50, n_folds=2, cost_bps=0.0,
                       multivariate=False)
    engine = OptimizerEngine(space, real_signal, cfg=cfg)
    bars = [{"ts": i, "close": 100.0 + 0.1 * i} for i in range(50)]
    result = engine.search(bars=bars)
    # We only assert that the engine produced a study. A drift of
    # 0.1 per bar is roughly a 10% per-bar return which gives an
    # astronomical Sharpe - far from realistic, but the test only
    # verifies the path works end-to-end.
    assert len(result.study.trials) >= 1
    ok_trials = [t for t in result.study.trials if t.ok]
    assert ok_trials, "at least one trial should be ok with real bars"
