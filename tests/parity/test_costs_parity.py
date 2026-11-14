"""Parity: local research/costs.py vs Blue_engine CostModel on a trade grid.

Uses the full Blue loader when it works (owner machine). Otherwise loads Blue's costs.py
alone with its helper imports stubbed, so the check also runs in the sandbox.
The leg mapping below is the bridge contract the old wrapper used.
"""
import pathlib

import pytest

from quant.core import blue_bridge
from quant.core.blue_bridge import BlueEngineUnavailable
from quant.research.costs import CostModel

REL = 1e-9
NOTIONALS = [1.0, 1_000.0, 60_000.0, 100_000.0, 10_000_000.0, 1_000_000_000.0]


BLUE_COSTS_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant" / "blue_engine" / "backtest" / "costs.py"


def _load_isolated(monkeypatch):
    """Load Blue's costs.py alone, with its helper imports stubbed (no OpenAlgo needed).

    monkeypatch keeps the stubs active for the whole test (the class imports its
    portfolio helper when a CostModel is built) and restores sys.modules afterwards.
    """
    import importlib.util, sys, types
    pkg = types.ModuleType("blue_engine.backtest")
    pkg.__path__ = []
    pkg.config = types.SimpleNamespace(DEFAULT_SLIPPAGE_BPS=5.0, DEFAULT_LATENCY_BPS=0.0)
    noop = lambda *a, **k: None  # noqa: E731
    pkg.logger = types.SimpleNamespace(info=noop, warning=noop, debug=noop)
    cost_aware = types.ModuleType("blue_engine.portfolio.optim.cost_aware")
    cost_aware.estimate_cost_bps = lambda cfg=None: dict(cfg or {})
    for name, mod in [
        ("blue_engine.backtest", pkg),
        ("blue_engine.portfolio", types.ModuleType("blue_engine.portfolio")),
        ("blue_engine.portfolio.optim", types.ModuleType("blue_engine.portfolio.optim")),
        ("blue_engine.portfolio.optim.cost_aware", cost_aware),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("blue_engine.backtest.costs", BLUE_COSTS_SRC)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod.CostModel


@pytest.fixture
def Blue(monkeypatch):
    """Blue's CostModel: full loader if it works, else loaded in isolation."""
    try:
        cls = blue_bridge.load("backtest.costs").CostModel
        cls().total_bps(is_buy=True)  # probe: builds the portfolio import chain
        return cls
    except (BlueEngineUnavailable, ImportError):
        return _load_isolated(monkeypatch)


def _blue_trade_cost(Blue, m: CostModel, notional, side, product):
    is_buy = side == "BUY"
    intraday = product == "intraday"
    if intraday:
        brokerage = min(m.brokerage_pct * 1e4, m.brokerage_cap / notional * 1e4)
        stt = 0.0 if is_buy else m.stt_intraday_sell * 1e4
        stamp = m.stamp_buy_intraday * 1e4 if is_buy else 0.0
    else:
        brokerage = m.delivery_brokerage_pct * 1e4
        stt = m.stt_delivery * 1e4
        stamp = m.stamp_buy_delivery * 1e4 if is_buy else 0.0
    cfg = {"brokerage": brokerage, "stt": stt, "exchange_txn": m.exchange_txn_pct * 1e4,
           "sebi": m.sebi_fee_pct * 1e4, "gst_on_brokerage_pct": m.gst_pct * 100.0,
           "stamp_duty": stamp}
    bm = Blue(cost_config=cfg, slippage_bps=m.impact_bps, latency_bps=0.0, impact_coefficient=0.0)
    return notional * bm.total_bps(is_buy=is_buy) / 10_000.0


@pytest.mark.parametrize("notional", NOTIONALS)
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("product", ["delivery", "intraday"])
def test_local_matches_blue(Blue, notional, side, product):
    m = CostModel()
    local = m.trade_cost(notional, side, product)
    blue = _blue_trade_cost(Blue, m, notional, side, product)
    assert local == pytest.approx(blue, rel=REL, abs=1e-9)
