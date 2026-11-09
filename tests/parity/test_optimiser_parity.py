"""Parity (stage 6.4 phases 1-2): local optimisers vs the real Blue_engine optimisers.

Runs in the sandbox through conftest.blue. Weights must match Blue within 1e-9 (the
algorithms are the same; this tolerance covers ERC iteration noise).
"""
import numpy as np
import pytest

from quant.portfolio.optimiser_base import ConstraintSpec as LocalSpec, project_to_box
from quant.portfolio.optimiser_methods import HRP, NCO, InverseVol, MaxSharpe, MinVariance, RiskParity


def _random_panel(seed, n_assets, n_obs):
    rng = np.random.default_rng(seed)
    scale = rng.uniform(0.5, 2.0, n_assets)
    return rng.normal(0, 0.01, (n_obs, n_assets)) * scale + rng.normal(0, 0.003, (n_obs, 1))


def _problem(seed):
    n = 2 + seed % 6
    names = [f"S{i}" for i in range(n)]
    S = np.cov(_random_panel(seed, n, 250), rowvar=False, ddof=1)
    mu = {nm: float(v) for nm, v in zip(names, np.random.default_rng(seed).normal(0.001, 0.002, n))}
    vols = {nm: float(np.sqrt(S[i, i])) for i, nm in enumerate(names)}
    return {"universe": names, "cov": S.tolist(), "mu": mu, "vols": vols, "risk_free": 0.0005}


def _assert_same(local_out, blue_out):
    assert local_out["ok"] == blue_out["ok"]
    if not blue_out["ok"]:
        return
    assert set(local_out["weights"]) == set(blue_out["weights"])
    for k, v in blue_out["weights"].items():
        assert local_out["weights"][k] == pytest.approx(v, abs=1e-9)


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("cls_name", ["MinVariance", "MaxSharpe", "RiskParity", "InverseVol", "HRP", "NCO"])
def test_method_matches_blue(blue, seed, cls_name):
    local_cls = {"MinVariance": MinVariance, "MaxSharpe": MaxSharpe, "RiskParity": RiskParity,
                 "InverseVol": InverseVol, "HRP": HRP, "NCO": NCO}[cls_name]
    mod = {"MinVariance": "portfolio.optim.mean_variance", "MaxSharpe": "portfolio.optim.mean_variance",
           "RiskParity": "portfolio.optim.risk_parity", "InverseVol": "portfolio.optim.risk_parity",
           "HRP": "portfolio.optim.hrp", "NCO": "portfolio.optim.hrp"}[cls_name]
    blue_cls = getattr(blue(mod), cls_name)
    problem = _problem(seed)
    _assert_same(local_cls().optimise(problem), blue_cls().optimise(problem))


@pytest.mark.parametrize("seed", range(10))
def test_projection_matches_blue(blue, seed):
    base = blue("portfolio.optim.base")
    rng = np.random.default_rng(seed)
    names = [f"S{i}" for i in range(5)]
    raw = rng.dirichlet(np.ones(5)) * rng.uniform(0.8, 1.6)
    weights = dict(zip(names, map(float, raw)))
    current = {nm: float(v) for nm, v in zip(names, rng.dirichlet(np.ones(5)))}
    betas = {nm: float(v) for nm, v in zip(names, rng.uniform(0.6, 1.4, 5))}
    kw = dict(min_weight=0.0, max_weight=0.4, max_gross=1.0, max_turnover=0.15,
              beta_lower=0.9, beta_upper=1.1)
    local_w, local_d = project_to_box(dict(weights), LocalSpec(**kw), current=current, betas=betas)
    blue_w, blue_d = base.project_to_box(dict(weights), base.ConstraintSpec(**kw),
                                         current=current, betas=betas)
    for k in names:
        assert local_w[k] == pytest.approx(blue_w[k], abs=1e-9)
    assert sorted(local_d["binding"]) == sorted(blue_d["binding"])
