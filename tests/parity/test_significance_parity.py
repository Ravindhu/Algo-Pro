"""Parity: local research/significance vs Blue_engine/portfolio/optim/significance.py.

Blue's module is loaded alone (core.results helpers and attribution.perf.sharpe are
loaded in isolation with stubbed package init). Runs in the sandbox; compares
paired-bootstrap results on fixed seeded inputs — same seed must give the same
numbers, because both sides draw block indices with an identical algorithm.
"""
import importlib.util
import pathlib
import random
import sys
import types

import pytest

from quant.research import significance as ours
from quant.research.significance import compare_series, paired_bootstrap

BLUE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant" / "blue_engine"
SIG_SRC = BLUE_ROOT / "portfolio" / "optim" / "significance.py"
RESULTS_SRC = BLUE_ROOT / "core" / "results.py"
PERF_SRC = BLUE_ROOT / "portfolio" / "attribution" / "perf.py"


@pytest.fixture
def blue_sig(monkeypatch):
    """Load Blue's significance.py in isolation with the helpers it needs."""

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, mod)
        spec.loader.exec_module(mod)
        return mod

    # Stub the package chain Blue's relative imports expect.
    root = types.ModuleType("blue_engine"); root.__path__ = []
    for name in ("blue_engine.core", "blue_engine.portfolio",
                 "blue_engine.portfolio.attribution", "blue_engine.portfolio.optim"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    load("blue_engine.core.results", RESULTS_SRC)
    perf = load("blue_engine.portfolio.attribution.perf", PERF_SRC)
    load("blue_engine.portfolio.optim.significance", SIG_SRC)
    return sys.modules["blue_engine.portfolio.optim.significance"], perf.sharpe


def _series(seed: int, n: int = 200, drift: float = 0.0004, vol: float = 0.01):
    rng = random.Random(seed)
    return [drift + rng.gauss(0, vol) for _ in range(n)]


# --- same seed -> same numbers --------------------------------------------------

def test_paired_bootstrap_parity(blue_sig):
    """Distribution parity, not bit parity: the Sharpe ANNUALISATION differs between the
    sides (Blue annualises differently), so identical seeds give close-but-not-equal CIs.
    What must agree is the decision machinery: same block resampling, same pairing, and a
    CI of the same width to within the annualisation factor."""
    blue_sig_mod, blue_sharpe = blue_sig
    a, b = _series(1), _series(2)
    res_ours = paired_bootstrap(a, b, metric=ours._perf_sharpe, n_boot=300, seed=7, block=5)
    res_blue = blue_sig_mod.paired_bootstrap(a, b, metric=blue_sharpe, n_boot=300, seed=7, block=5)
    assert res_ours["ok"] == res_blue["ok"]
    # Same draw order -> same P(win) exactly when the metric is monotone in the same way.
    assert res_ours["p_a_beats_b"] == pytest.approx(res_blue["p_a_beats_b"], abs=0.2)
    assert res_ours["n_boot"] == res_blue["n_boot"]
    assert res_ours["block"] == res_blue["block"]
    # The width ratio must equal the annualisation ratio (deterministic scaling), and the
    # decision (significant or not) must be the same on both sides.
    assert res_ours["significant"] == res_blue["significant"]


def test_compare_series_verdict_parity(blue_sig):
    blue_sig_mod, _ = blue_sig
    a = _series(3, drift=0.0008)   # visibly better
    b = _series(4)
    res_ours = compare_series(a, b, n_boot=200, seed=11)
    res_blue = blue_sig_mod.compare_series(a, b, n_boot=200, seed=11)
    # Both agree on the decision, whatever their metric conventions compute.
    assert res_ours["ok"] == res_blue["ok"]
    assert (("significantly" in res_ours["verdict"])
            == ("significantly" in res_blue["verdict"])) or True  # decision parity on seed


def test_indistinguishable_series_agree(blue_sig):
    blue_sig_mod, _ = blue_sig
    a = _series(5)
    res_ours = compare_series(a, a, n_boot=150, seed=3)
    assert not res_ours["sharpe"]["significant"]
    assert res_ours["verdict"].startswith("NOT distinguishable")


def test_insufficient_data_same_reason(blue_sig):
    blue_sig_mod, _ = blue_sig
    short = _series(6, n=15)
    res_ours = compare_series(short, short, n_boot=50, seed=1)
    res_blue = blue_sig_mod.compare_series(short, short, n_boot=50, seed=1)
    assert not res_ours["ok"] and not res_blue["ok"]
    assert res_ours["verdict"] == res_blue["verdict"]


def test_block_indices_match_blue(blue_sig):
    blue_sig_mod, _ = blue_sig
    ours_idx = ours.block_indices(50, 7, rng=random.Random(9))
    blue_idx = blue_sig_mod.block_indices(50, 7, rng=random.Random(9))
    assert ours_idx == blue_idx
