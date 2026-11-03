"""Phase 0: rebalance no-trade decision delegated to Blue_engine; boundary policy kept local.

Needs the OpenAlgo SDK (Blue_engine imports it). Skips with the reason when absent.
The oracle below is the pre-wrap formula, written out here to pin the old behaviour.
"""
import numpy as np
import pytest

from quant.core import blue_bridge
from quant.core.blue_bridge import BlueEngineUnavailable


@pytest.fixture(autouse=True)
def _need_blue():
    try:
        blue_bridge.load("portfolio.optim.cost_aware")
    except BlueEngineUnavailable as exc:
        pytest.skip(f"Blue_engine not importable here: {exc}")


def _pre_wrap(current, target, band):
    out = {}
    for k in set(current) | set(target):
        d = current.get(k, 0.0) - target.get(k, 0.0)
        if abs(d) > band:
            out[k] = -d
    return out


def test_matches_pre_wrap_formula_on_random_portfolios():
    from quant.portfolio.rebalance import rebalance_trades

    rng = np.random.default_rng(2)
    for _ in range(300):
        names = [f"S{i}" for i in range(int(rng.integers(1, 8)))]
        target = {s: float(rng.uniform(0, 0.4)) for s in names}
        current = {s: float(max(0.0, target[s] + rng.normal(0, 0.03))) for s in names}
        band = float(rng.choice([0.01, 0.02, 0.05]))
        got = rebalance_trades(current, target, band)
        want = _pre_wrap(current, target, band)
        assert set(got) == set(want)
        for k in want:
            assert got[k] == pytest.approx(want[k], abs=1e-6)  # Blue rounds to 6 dp


def test_drift_exactly_on_band_does_not_trade():
    from quant.portfolio.rebalance import rebalance_trades

    assert rebalance_trades({"A": 0.0}, {"A": 0.02}, band=0.02) == {}


def test_drift_just_outside_band_trades_with_correct_sign():
    from quant.portfolio.rebalance import rebalance_trades

    trades = rebalance_trades({"A": 0.0}, {"A": 0.0201}, band=0.02)
    assert trades["A"] == pytest.approx(0.0201, abs=1e-6) and trades["A"] > 0
    trades = rebalance_trades({"A": 0.0201}, {"A": 0.0}, band=0.02)
    assert trades["A"] == pytest.approx(-0.0201, abs=1e-6) and trades["A"] < 0


def test_decision_is_delegated_to_blue(monkeypatch):
    cost = blue_bridge.load("portfolio.optim.cost_aware")
    calls = []
    real = cost.should_rebalance

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(cost, "should_rebalance", spy)
    from quant.portfolio.rebalance import rebalance_trades

    rebalance_trades({"A": 0.3, "B": 0.7}, {"A": 0.3, "B": 0.7}, band=0.02)
    assert calls and calls[0]["band"] == {"A": 0.02, "B": 0.02}
