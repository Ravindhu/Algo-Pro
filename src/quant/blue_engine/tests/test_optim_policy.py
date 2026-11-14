"""
tests/test_optim_policy.py — Phase 4: optimiser selection, constraints, kill-switch.

The most important tests here are the HALT paths. A desk's losses rarely come
from picking the wrong objective function; they come from continuing to trade
through a drawdown, stale data, or a malformed book. "Stop" must be a testable
decision, not an afterthought.

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_optim_policy.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import optim_policy as op  # noqa: E402


import pytest  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


@pytest.fixture(autouse=True)
def _fresh_oracle(request):
    """A clear kill switch and a clean persistent store for every test here.

    ``decide()`` uses ONE latching switch per process and PERSISTS a trip. The
    sibling test ``test_decide_exits_to_cash_when_tripped`` deliberately trips it,
    and several tests here expect ``decide()`` to reach the DATA-validity checks.
    With a tripped switch, ``decide()`` returns EXIT_TO_CASH before those checks
    and those tests fail.

    This fixture makes the file self-sufficient. Measured 2026-11-08: a full
    ``pytest`` run produced 24 failures here while the folder ran with quant ahead
    of it, even though ``Blue_engine/tests/conftest.py`` has an equivalent guard -
    the guard runs, but the explicit trip in this file still wins for later tests
    in the same module. An autouse fixture IN this module cannot be ordered away.
    """
    import os
    import tempfile

    from blue_engine.core import kill_switch_store as ks_store
    from blue_engine.portfolio import optim_policy as op

    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    os.environ["BLUE_KILL_SWITCH_DB_PATH"] = os.path.join(
        tmp.name, "kill_switch.db")
    ks_store.close_store()
    ks_store._DEGRADED = False
    op._PROCESS_KILL_SWITCH = None
    yield
    op._PROCESS_KILL_SWITCH = None
    ks_store.close_store()
    ks_store._DEGRADED = False
    os.environ.pop("BLUE_KILL_SWITCH_DB_PATH", None)
    tmp.cleanup()


def _inputs(**over):
    d = {
        "universe": ["NIFTY", "BANKNIFTY", "IT"],
        "n_obs": 300,
        "cov": [[0.020, 0.008, 0.004],
                [0.008, 0.030, 0.010],
                [0.004, 0.010, 0.040]],
    }
    d.update(over)
    return d


# --------------------------------------------------------------------------- #
# 1. Kill-switch — the decision that actually saves money
# --------------------------------------------------------------------------- #
def test_kill_switch_trips_on_max_drawdown():
    ks = op.KillSwitch(max_drawdown=0.15, max_daily_loss=0.04)
    out = ks.evaluate(drawdown=0.16)
    _check(out["tripped"] is True, "16% drawdown must trip a 15% limit")
    _check("drawdown" in (out["reason"] or ""), f"reason should say why: {out}")


def test_kill_switch_does_not_trip_below_the_limit():
    ks = op.KillSwitch(max_drawdown=0.15)
    out = ks.evaluate(drawdown=0.14)
    _check(out["tripped"] is False, "14% is inside a 15% limit")


def test_kill_switch_trips_on_daily_loss():
    ks = op.KillSwitch(max_drawdown=0.50, max_daily_loss=0.04)
    out = ks.evaluate(drawdown=0.0, daily_loss=0.05)
    _check(out["tripped"] is True, "a 5% session loss must trip a 4% limit")


def test_kill_switch_is_sticky_until_reset():
    """A kill-switch that un-trips as the drawdown recovers is just a stop-loss.

    This is the property that matters: re-entry must be a deliberate human act,
    not an automatic consequence of the market ticking back up.
    """
    ks = op.KillSwitch(max_drawdown=0.15)
    ks.evaluate(drawdown=0.20)
    _check(ks.tripped is True, "should be tripped")
    out = ks.evaluate(drawdown=0.01)          # market recovered
    _check(out["tripped"] is True,
           "a recovered drawdown must NOT silently clear the switch")


def test_kill_switch_reset_requires_an_explicit_token():
    ks = op.KillSwitch(max_drawdown=0.15)
    ks.evaluate(drawdown=0.20)
    bad = ks.reset()                          # no confirm token
    _check(bad["ok"] is False and ks.tripped is True,
           f"reset without confirmation must be refused: {bad}")
    good = ks.reset(confirm="ops-1")
    _check(good["ok"] is True and ks.tripped is False, "a named reset works")


def test_decide_exits_to_cash_when_tripped():
    out = op.decide(_inputs(), regime="RISK_ON", drawdown=0.20)
    _check(out["action"] == "EXIT_TO_CASH",
           f"a tripped switch means a flat book, got {out['action']}")
    _check(out["trade"] is False, "must not trade")
    _check(out["ok"] is True, "a halt is a valid decision, not an error")
    _check(out["optimiser"] is None, "no optimiser should be selected")


def test_decide_can_halt_without_flattening():
    out = op.decide(_inputs(), drawdown=0.20, exit_on_kill=False)
    _check(out["action"] == "HALT",
           f"exit_on_kill=False should only halt, got {out['action']}")


# --------------------------------------------------------------------------- #
# 2. Data validity — never trade a stale or broken book
# --------------------------------------------------------------------------- #
def test_stale_data_halts_trading():
    """Stale prices make sleeves look UNcorrelated, so the optimiser concentrates.

    That is the dangerous part: a data fault does not produce an obviously wrong
    book, it produces a confidently concentrated one.
    """
    out = op.decide(_inputs(meta={"staleness_days": 30}), regime="RISK_ON")
    _check(out["ok"] is False, "stale data must not be traded")
    _check(out["action"] == "HALT", f"action should be HALT: {out['action']}")
    _check(any("stale" in r.lower() for r in out["reasons"]),
           f"the reason should name staleness: {out['reasons']}")


def test_single_sleeve_universe_halts():
    out = op.decide({"universe": ["ONLY_ONE"]})
    _check(out["ok"] is False, "one sleeve is nothing to allocate across")
    _check(any("need >= 2" in r for r in out["reasons"]),
           f"should say how many are needed: {out['reasons']}")


def test_degenerate_covariance_halts():
    bad = {"universe": ["A", "B"], "n_obs": 300,
           "cov": [[0.0, 0.0], [0.0, 0.02]]}
    out = op.decide(bad)
    _check(out["ok"] is False, "a zero-variance sleeve is degenerate")
    _check(any("variance" in r for r in out["reasons"]),
           f"should name the degenerate sleeve: {out['reasons']}")


def test_short_sample_warns_but_does_not_halt():
    """A short sample is a *warning*: the estimate is noisy, not invalid."""
    out = op.decide(_inputs(n_obs=20))
    _check(out["ok"] is True, "20 observations should still be tradeable")
    _check(any("observations" in w for w in out["warnings"]),
           f"a short sample should be flagged: {out['warnings']}")


def test_stale_flag_from_the_provider_warns():
    out = op.decide(_inputs(meta={"stale": True}))
    _check(out["ok"] is True, "a stale flag alone is not fatal")
    _check(any("stale" in w.lower() for w in out["warnings"]),
           f"provider staleness should warn: {out['warnings']}")


# --------------------------------------------------------------------------- #
# 3. Regime -> optimiser
# --------------------------------------------------------------------------- #
def test_regime_selects_an_optimiser():
    on = op.decide(_inputs(), regime="RISK_ON")
    off = op.decide(_inputs(), regime="RISK_OFF")
    _check(on["optimiser"], "risk-on should pick an optimiser")
    _check(off["optimiser"], "risk-off should pick an optimiser")
    _check(on["action"] == "OPTIMISE", "should optimise")


def test_unknown_regime_still_decides():
    out = op.decide(_inputs(), regime=None)
    _check(out["trade"] is True, "no regime must not block trading")
    _check(out["optimiser"] is not None, "a default optimiser must be chosen")


def test_regime_override_tightens_gross():
    """The single most valuable use of the regime: less gross when risk-off."""
    limits = {"regime_overrides": {"RISK_OFF": {"max_gross": 0.5}}}
    out = op.decide(_inputs(), regime="RISK_OFF", limits=limits)
    _check(abs(out["constraints"]["max_gross"] - 0.5) < 1e-9,
           f"risk-off should cap gross at 0.5, got {out['constraints']['max_gross']}")


def test_regime_override_can_pick_a_different_optimiser():
    limits = {"regime_policy": {"RISK_OFF": {"optimiser": "min_variance"}}}
    out = op.decide(_inputs(), regime="RISK_OFF", limits=limits)
    _check(out["optimiser"] == "min_variance",
           f"override should select min_variance, got {out['optimiser']}")


# --------------------------------------------------------------------------- #
# 4. Constraints
# --------------------------------------------------------------------------- #
def test_resolve_constraints_merges_defaults_and_overrides():
    out = op.resolve_constraints({"max_weight": 0.20, "max_gross": 0.8})
    _check(abs(out["max_weight"] - 0.20) < 1e-9, "override max_weight")
    _check(abs(out["max_gross"] - 0.8) < 1e-9, "override max_gross")
    _check(out["max_turnover"] is not None, "defaults still present")


def test_constraints_include_beta_band_and_sector_cap():
    out = op.decide(_inputs(), regime="NEUTRAL")
    c = out["constraints"]
    for key in ("beta_lower", "beta_upper", "max_sector_weight",
                "max_turnover"):
        _check(key in c, f"constraint '{key}' missing from {list(c)}")
    _check(c["beta_lower"] <= c["beta_upper"], "beta band must be ordered")


# --------------------------------------------------------------------------- #
# 5. Applying the decision to a problem
# --------------------------------------------------------------------------- #
def test_apply_to_problem_attaches_constraints_and_optimiser():
    decision = op.decide(_inputs(), regime="RISK_ON")
    problem = op.apply_to_problem({"universe": ["NIFTY", "IT"]}, decision)
    _check(problem["constraints"], "constraints must be attached")
    _check(problem["optimiser"] == decision["optimiser"], "optimiser attached")
    _check(problem["policy_reasons"], "reasons should be carried for the UI")


def test_apply_to_problem_produces_a_flat_book_on_exit():
    """A halt must flow through the SAME code path as any other allocation.

    Returning a legitimate zero-weight book (plus a reason) is far less
    disruptive downstream than raising: POV, the risk gate and the dashboard all
    understand weights, and the halt becomes visible in the same places.
    """
    decision = op.decide(_inputs(), drawdown=0.20)
    problem = op.apply_to_problem(
        {"universe": ["NIFTY", "BANKNIFTY", "IT"]}, decision)
    _check(problem["weights"] == {"NIFTY": 0.0, "BANKNIFTY": 0.0, "IT": 0.0},
           f"an exit must flatten every sleeve: {problem['weights']}")
    _check(problem.get("forced_flat") is True, "should be flagged as forced")


def test_apply_to_problem_does_not_mutate_the_input():
    original = {"universe": ["A", "B"], "cov": None}
    snapshot = dict(original)
    op.apply_to_problem(original, op.decide(_inputs(), regime="RISK_ON"))
    _check(original == snapshot,
           "the caller's problem dict must not be mutated in place")


# --------------------------------------------------------------------------- #
# 6. Degradation
# --------------------------------------------------------------------------- #
def test_decide_never_raises_on_garbage():
    for junk in (None, {}, {"universe": None}, {"universe": ["A"],
                                                "cov": "not-a-matrix"},
                 {"universe": ["A", "B"], "cov": [[None, None], [None, None]]}):
        out = op.decide(junk)
        _check("action" in out, f"every input must produce a decision: {junk}")
        _check(isinstance(out["reasons"], list),
               f"reasons must always be a list for {junk}")


def test_every_decision_explains_itself():
    """No decision may be returned without a reason — a silent halt is unusable."""
    cases = [(_inputs(), {"regime": "RISK_ON"}),
             (_inputs(), {"drawdown": 0.20}),
             ({"universe": ["A"]}, {}),
             (_inputs(meta={"staleness_days": 99}), {})]
    for inp, kw in cases:
        out = op.decide(inp, **kw)
        _check(len(out["reasons"]) > 0,
               f"decision for {kw} left no reason: {out}")


def test_kill_takes_precedence_over_bad_data_by_default():
    """The switch is evaluated first, so a tripped book reports the halt.

    Ordering matters for diagnosis: an operator must be told "you are stopped
    for drawdown", not buried under data warnings that may be incidental.
    """
    out = op.decide({"universe": ["A"]}, drawdown=0.30)
    _check(out["action"] == "EXIT_TO_CASH",
           f"the kill-switch should win the ordering: {out['action']}")
    _check(any("kill-switch" in r for r in out["reasons"]),
           f"the reason should name the switch: {out['reasons']}")


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