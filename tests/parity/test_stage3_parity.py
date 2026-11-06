"""Parity (stage 3): local ports vs the real Blue_engine functions, on random and edge inputs.

Runs in the sandbox through conftest.blue (no openalgo needed). Covers:
profit factor, historical CVaR, rebalance no-trade rule, fractional Kelly, vol-target scale,
shrunk covariance (constant-correlation target, supplied delta).
"""
import numpy as np
import pytest

from quant.portfolio.covariance import shrink_cov
from quant.portfolio.portfolio_risk import historical_cvar
from quant.portfolio.rebalance import rebalance_trades
from quant.portfolio.sizing import fractional_kelly, vol_target_scale
from quant.reporting.dashboard import profit_factor


def test_profit_factor_matches_blue(blue):
    bm = blue("backtest.metrics")
    for gp, gl in [(300.0, -100.0), (0.0, -5.0), (5.0, 0.0), (1e6, -3.3e5), (0.0, 0.0)]:
        assert profit_factor(gp, gl) == bm.profit_factor(gp, gl)


@pytest.mark.parametrize("seed,alpha", [(1, 0.05), (2, 0.01), (3, 0.10), (4, 0.5)])
def test_cvar_matches_blue(blue, seed, alpha):
    tail = blue("portfolio.risk.tail")
    rng = np.random.default_rng(seed)
    pnl = list(rng.normal(0, 0.02, 300))
    pnl[5] = None
    pnl[9] = float("nan")
    want = tail.cvar(pnl, level=1.0 - alpha)
    assert historical_cvar(pnl, alpha=alpha) == pytest.approx(want, rel=1e-12, abs=1e-15)


def test_cvar_too_few_outcomes_refused(blue):
    assert blue("portfolio.risk.tail").cvar([0.1]) is None
    with pytest.raises(ValueError):
        historical_cvar([0.1])


@pytest.mark.parametrize("seed", range(6))
def test_rebalance_matches_blue_no_trade_rule(blue, seed):
    cost = blue("portfolio.optim.cost_aware")
    rng = np.random.default_rng(seed)
    names = [f"S{i}" for i in range(8)]
    target = {n: float(w) for n, w in zip(names, rng.dirichlet(np.ones(8)))}
    current = {n: max(0.0, w + rng.normal(0, 0.03)) for n, w in target.items()}
    band = 0.02
    blue_moves = cost.should_rebalance(target, current, band={n: band for n in names})["moves"]
    want = {k: v for k, v in blue_moves.items() if abs(current.get(k, 0) - target.get(k, 0)) > band}
    got = rebalance_trades(current, target, band=band)
    assert set(got) == set(want)
    for k in want:
        assert got[k] == pytest.approx(want[k], abs=1e-12)


@pytest.mark.parametrize("mu,sigma", [(0.01, 0.2), (0.2, 0.2), (-0.02, 0.15), (0.05, 0.02), (0.01, 0.0)])
def test_fractional_kelly_matches_blue_formula(blue, mu, sigma):
    kelly = blue("portfolio.sizing.kelly")
    full = kelly.kelly_leverage(mu, 0.0, sigma)
    want = 0.0 if full is None else min(0.10, max(0.0, 0.25 * full))
    assert fractional_kelly(mu, sigma) == pytest.approx(want, abs=1e-15)


@pytest.mark.parametrize("realized,target,max_lev", [
    (0.20, 0.10, 1.0), (0.05, 0.10, 1.0), (0.10, 0.10, 1.5), (0.04, 0.30, 2.0), (0.5, 0.01, 1.0),
])
def test_vol_target_matches_blue(blue, realized, target, max_lev):
    vt = blue("portfolio.sizing.vol_target")
    want = vt.vol_target_scale(realized, target, min_scale=0.0, max_scale=max_lev)["scale"]
    assert vol_target_scale(realized, target, max_leverage=max_lev) == pytest.approx(want, abs=1e-12)


def test_vol_target_floor_quirk_matches_blue(blue):
    """Blue returns scale 1.0 for 0 < vol <= 1e-6. Kept on purpose; see sizing.py docstring."""
    vt = blue("portfolio.sizing.vol_target")
    want = vt.vol_target_scale(1e-7, 0.1, min_scale=0.0, max_scale=1.0)["scale"]
    assert want == 1.0
    assert vol_target_scale(1e-7, 0.1, max_leverage=1.0) == 1.0


@pytest.mark.parametrize("seed,delta", [(11, 0.0), (12, 0.3), (13, 0.7), (14, 1.0)])
def test_shrink_cov_matches_blue_ledoit_wolf(blue, seed, delta):
    cov_mod = blue("portfolio.risk.covariance")
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, 0.01, (120, 5))          # time x asset
    returns[3, 2] = np.nan                            # missing value: pair dropped in both
    want, _ = cov_mod.ledoit_wolf_cov(returns.T.tolist(), annualise=False, shrink=delta)
    got = shrink_cov(returns, delta)
    # Tolerance: rel 1e-9, plus an absolute floor of 1e-8 x largest entry. Measured worst case over
    # these seeds: 4.5e-13 abs on a 1.2e-4 matrix (~4e-9 of scale), from Jacobi (Blue) vs LAPACK
    # (local) eigen solvers. Economically negligible; recorded in docs/DECOUPLING_PLAN.md.
    want = np.asarray(want)
    floor = 1e-8 * float(np.max(np.abs(want)))
    assert np.allclose(got, want, rtol=1e-9, atol=floor)
