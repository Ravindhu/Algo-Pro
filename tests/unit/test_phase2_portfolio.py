"""Phase 2: sizing, allocation, margin, covariance, HRP, rebalancing, portfolio risk, sign-off gates."""
import json
from pathlib import Path

import numpy as np
import pytest

from quant.feedback.champion_challenger import ModelRegistry
from quant.feedback.reopt import ReoptQueue
from quant.monitor.drift import psi
from quant.monitor.parity import parity_report
from quant.portfolio.allocator import allocate, funds_available_t1, margin_ok
from quant.portfolio.covariance import shrink_cov
from quant.portfolio.optimizer import hrp_weights
from quant.portfolio.portfolio_risk import (
    beta_exposure, cap_violations, group_exposure, historical_cvar, stress_pnl,
)
from quant.portfolio.rebalance import rebalance_trades
from quant.portfolio.sizing import fractional_kelly, vol_target_scale
from quant.data.market_inputs import order_book_imbalance, relative_strength
import pandas as pd


def test_kelly_is_fractional_and_capped():
    assert 0 < fractional_kelly(0.001, 0.1) < 0.10  # 0.25 * 0.001 / 0.01 = 0.025
    assert fractional_kelly(10, 0.01) == 0.10
    assert fractional_kelly(-0.01, 0.01) == 0.0
    assert vol_target_scale(0.4, 0.2, max_leverage=1.0) == 0.5
    assert vol_target_scale(0.1, 0.2, max_leverage=1.0) == 1.0


def test_allocation_respects_capacity_and_budget():
    out = allocate(1_000_000, {"a": 2.0, "b": 1.0, "c": -1.0}, {"a": 300_000, "b": 900_000, "c": 900_000})
    assert out["c"] == 0.0
    assert out["a"] == 300_000
    assert sum(out.values()) <= 1_000_000


def test_margin_and_t1_funds():
    assert margin_ok(100_000, 0.2, 25_000)
    assert not margin_ok(100_000, 0.2, 10_000)
    assert funds_available_t1(50_000, 30_000, reserved=10_000) == 70_000


def test_shrinkage_endpoints():
    rng = np.random.default_rng(6)
    R = rng.normal(0, 0.01, (300, 4))
    S = np.cov(R, rowvar=False, ddof=1)
    assert np.allclose(shrink_cov(R, 0.0), S)
    F = shrink_cov(R, 1.0)
    corr = F / np.outer(np.sqrt(np.diag(F)), np.sqrt(np.diag(F)))
    off = corr[~np.eye(4, dtype=bool)]
    assert np.allclose(off, off[0])
    with pytest.raises(ValueError):
        shrink_cov(R, 1.5)


def test_hrp_weights_sum_to_one_and_penalise_volatility():
    cov = np.diag([0.01, 0.04, 0.02])
    w = hrp_weights(cov)
    assert w.sum() == pytest.approx(1.0) and (w >= 0).all()
    assert w[1] < w[0] < w[2] or w[1] < w[2] < w[0] or w[1] == min(w)
    assert hrp_weights(np.array([[0.01]])).tolist() == [1.0]


def test_rebalance_band_ignores_small_drift():
    trades = rebalance_trades({"A": 0.31, "B": 0.69}, {"A": 0.30, "B": 0.70}, band=0.02)
    assert trades == {}
    trades = rebalance_trades({"A": 0.40, "B": 0.60}, {"A": 0.30, "B": 0.70}, band=0.02)
    assert trades["A"] == pytest.approx(-0.10) and trades["B"] == pytest.approx(0.10)


def test_portfolio_risk_caps_stress_and_cvar():
    w = {"A": 0.5, "B": 0.4, "C": 0.1}
    group = {"A": "BANK", "B": "BANK", "C": "IT"}
    assert group_exposure(w, group)["BANK"] == pytest.approx(0.9)
    assert cap_violations(w, group, cap=0.3)
    assert beta_exposure(w, {"A": 1.2, "B": 0.8}) == pytest.approx(0.5 * 1.2 + 0.4 * 0.8 + 0.1)
    assert stress_pnl(w, {"A": -0.3}) == pytest.approx(-0.15)
    pnl = np.linspace(-0.1, 0.1, 100)
    assert historical_cvar(pnl, 0.05) > 0.08


def test_registry_requires_signoff_and_dss_rule(tmp_path):
    reg = ModelRegistry(tmp_path / "reg.json")
    reg.register("v1", {"dsr": 0.9})
    with pytest.raises(PermissionError):
        reg.promote("v1")
    reg.sign_off("v1", "ravi", "reviewed OOS results")
    reg.promote("v1")
    assert reg.champion == "v1"
    reg.register("v2", {"dsr": 0.5})
    reg.sign_off("v2", "ravi", "ok")
    with pytest.raises(PermissionError):
        reg.promote("v2")  # worse DSR than champion
    assert ModelRegistry(tmp_path / "reg.json").champion == "v1"


def test_reopt_needs_approval(tmp_path):
    q = ReoptQueue(tmp_path / "q.json")
    rid = q.request("S1", "2025-01-01", "2026-01-01", "regime change")
    assert q.runnable() == []
    with pytest.raises(ValueError):
        q.approve(rid, "", "")
    q.approve(rid, "ravi", "run it")
    assert [i["id"] for i in q.runnable()] == [rid]


def test_psi_identical_small_and_shifted_large():
    rng = np.random.default_rng(7)
    base = rng.normal(0, 1, 5000)
    assert psi(base, rng.normal(0, 1, 5000)) < 0.05
    assert psi(base, rng.normal(1.5, 1, 5000)) > 0.25


def test_parity_report():
    rep = parity_report({"a": 100.0, "b": 200.0}, {"a": 100.05, "b": 200.1}, tol_bps=10)
    assert rep["matched"] == 2 and rep["passed"]
    assert not parity_report({"a": 101.0}, {"a": 100.0}, tol_bps=10)["passed"]


def test_market_input_helpers():
    assert order_book_imbalance(300, 100) == pytest.approx(0.5)
    assert order_book_imbalance(0, 0) == 0.0
    idx = pd.Series(np.linspace(100, 110, 100))
    stock = pd.Series(np.linspace(100, 130, 100))
    assert relative_strength(stock, idx, window=10).dropna().iloc[-1] > 0


def test_no_llm_code_in_risk_execution_or_portfolio():
    """Frontline AI may give advice, but never reach the risk, execution or portfolio code."""
    root = Path(__file__).resolve().parents[2] / "src" / "quant"
    for folder in ("risk", "execution", "portfolio"):
        for py in (root / folder).rglob("*.py"):
            text = py.read_text()
            assert "core.llm" not in text and "openai" not in text.lower(), py
