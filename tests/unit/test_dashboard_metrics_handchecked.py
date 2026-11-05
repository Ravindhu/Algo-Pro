"""X-15: EV, profit factor, win rate and MAE/MFE checked by hand on fixed trades.

Flow stage: Dashboard, Live monitor
Status: TEST

Expected numbers are worked out by hand in the comments. They are not copied
from the code. Trade list (return, rupee P&L):
  T1 ( +2%, +2000)   T2 ( -1%, -1000)   T3 ( +3%, +3000)
  T4 ( -2%, -2000)   T5 ( +1%, +1000)
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from quant.core import blue_bridge
from quant.core.blue_bridge import BlueEngineUnavailable
from quant.monitor.live_monitor import excursions
from quant.reporting import dashboard as dashboard_mod
from quant.reporting.dashboard import build_payload

METRICS_SRC = Path(__file__).resolve().parents[2] / "Blue_engine" / "backtest" / "metrics.py"


@pytest.fixture(autouse=True)
def blue_profit_factor(monkeypatch):
    """Blue's backtest.metrics: the real one if it loads, else loaded in isolation.

    metrics.py imports two other Blue modules at load time (ml.evaluation.metrics,
    portfolio.risk.tail). Those pull in OpenAlgo, so they are stubbed here. The
    profit_factor function does not use them.
    """
    try:
        blue_bridge.load("backtest.metrics").profit_factor(1.0, -1.0)
        return
    except (BlueEngineUnavailable, ImportError):
        pass
    noop = lambda *a, **k: None  # noqa: E731
    stubs = {
        "blue_engine.backtest": types.SimpleNamespace(logger=types.SimpleNamespace(
            info=noop, warning=noop, debug=noop)),
        "blue_engine.ml": types.ModuleType("blue_engine.ml"),
        "blue_engine.ml.evaluation": types.ModuleType("blue_engine.ml.evaluation"),
        "blue_engine.ml.evaluation.metrics": types.ModuleType("blue_engine.ml.evaluation.metrics"),
        "blue_engine.portfolio": types.ModuleType("blue_engine.portfolio"),
        "blue_engine.portfolio.risk": types.ModuleType("blue_engine.portfolio.risk"),
        "blue_engine.portfolio.risk.tail": types.ModuleType("blue_engine.portfolio.risk.tail"),
    }
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("blue_engine.backtest.metrics", METRICS_SRC)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(dashboard_mod, "load", lambda name: mod)

TRADES = [
    {"ret": 0.02, "pnl": 2000.0},
    {"ret": -0.01, "pnl": -1000.0},
    {"ret": 0.03, "pnl": 3000.0},
    {"ret": -0.02, "pnl": -2000.0},
    {"ret": 0.01, "pnl": 1000.0},
]


def test_fixed_trade_list_payload():
    p = build_payload(TRADES, risk_used=0.3, rule_breaches=1)
    # Sum of returns = 2 - 1 + 3 - 2 + 1 = 3% -> mean 0.6% per trade
    assert p["ev_pct_per_trade"] == pytest.approx(0.6)
    # Wins 3 of 5 trades
    assert p["win_rate"] == pytest.approx(0.6)
    # Gross profit 2000 + 3000 + 1000 = 6000; gross loss 1000 + 2000 = 3000 -> PF 2.0
    assert p["profit_factor"] == pytest.approx(2.0)
    # Net P&L 6000 - 3000 = 3000
    assert p["total_pnl"] == pytest.approx(3000.0)
    assert p["trades"] == 5
    assert p["risk_utilisation"] == 0.3
    assert p["rule_breaches"] == 1


def test_ev_equals_win_rate_times_avg_win_plus_loss_rate_times_avg_loss():
    # Second route to the same EV: 0.6 * avg win + 0.4 * avg loss
    # avg win = (2% + 3% + 1%) / 3 = 2%; avg loss = (-1% - 2%) / 2 = -1.5%
    # 0.6 * 2% + 0.4 * (-1.5%) = 1.2% - 0.6% = 0.6%
    p = build_payload(TRADES, risk_used=0.0, rule_breaches=0)
    assert p["ev_pct_per_trade"] == pytest.approx(0.6)


def test_break_even_trade_is_not_a_win_and_not_a_loss():
    trades = [{"ret": 0.0, "pnl": 0.0}, {"ret": 0.01, "pnl": 100.0}, {"ret": -0.01, "pnl": -50.0}]
    p = build_payload(trades, risk_used=0.0, rule_breaches=0)
    # Win rate: 1 win of 3 trades
    assert p["win_rate"] == pytest.approx(1 / 3)
    # PF: 100 / 50 = 2.0 (the zero trade adds to neither side)
    assert p["profit_factor"] == pytest.approx(2.0)
    # EV: (0 + 1% - 1%) / 3 = 0
    assert p["ev_pct_per_trade"] == pytest.approx(0.0)


def test_all_winners_has_no_profit_factor():
    trades = [{"ret": 0.01, "pnl": 100.0}, {"ret": 0.02, "pnl": 200.0}]
    p = build_payload(trades, risk_used=0.0, rule_breaches=0)
    assert p["profit_factor"] is None  # no losses, so the ratio is undefined
    assert p["win_rate"] == pytest.approx(1.0)
    assert p["ev_pct_per_trade"] == pytest.approx(1.5)


def test_no_trades_reports_none_not_zero():
    p = build_payload([], risk_used=0.2, rule_breaches=0)
    assert p["trades"] == 0
    assert p["ev_pct_per_trade"] is None
    assert p["win_rate"] is None
    assert p["profit_factor"] is None
    assert p["total_pnl"] == 0.0
    assert p["risk_utilisation"] == 0.2


# --- MAE / MFE: entry 100, path 98 -> 103 -> 96 -> 110 -> 105 ---------------------

PATH = [98, 103, 96, 110, 105]


def test_long_mae_and_mfe():
    # Moves vs 100: -2%, +3%, -4%, +10%, +5%
    # MAE = worst adverse move = 4% (the drop to 96). MFE = best = 10% (the peak at 110).
    mae, mfe = excursions(100, PATH, side=1)
    assert mae == pytest.approx(0.04)
    assert mfe == pytest.approx(0.10)


def test_short_mae_and_mfe_flip_direction():
    # For a short, a price rise is adverse. Same path: MAE = 10% (up to 110), MFE = 4% (down to 96).
    mae, mfe = excursions(100, PATH, side=-1)
    assert mae == pytest.approx(0.10)
    assert mfe == pytest.approx(0.04)


def test_excursion_uses_the_path_not_the_final_price():
    # Final price 105 is a gain for a long, yet MAE is still 4% from the dip to 96.
    mae, _ = excursions(100, PATH, side=1)
    assert mae == pytest.approx(0.04)


def test_never_adverse_long_has_zero_mae():
    mae, mfe = excursions(100, [101, 102], side=1)
    assert mae == 0.0
    assert mfe == pytest.approx(0.02)


def test_payload_shows_average_mae_and_mfe_in_percent():
    # Per-trade MAE/MFE as fractions: MAE 1%, 3%; MFE 2%, 4% -> averages 2% and 3%
    trades = [
        {"ret": 0.01, "pnl": 100.0, "mae": 0.01, "mfe": 0.02},
        {"ret": -0.01, "pnl": -50.0, "mae": 0.03, "mfe": 0.04},
    ]
    p = build_payload(trades, risk_used=0.0, rule_breaches=0)
    assert p["avg_mae_pct"] == pytest.approx(2.0)
    assert p["avg_mfe_pct"] == pytest.approx(3.0)


def test_mae_mfe_are_none_when_trades_lack_them():
    # Partial data must not give a misleading average
    trades = [{"ret": 0.01, "pnl": 100.0, "mae": 0.01}, {"ret": 0.02, "pnl": 200.0}]
    p = build_payload(trades, risk_used=0.0, rule_breaches=0)
    assert p["avg_mae_pct"] is None
    assert p["avg_mfe_pct"] is None


def test_never_favourable_long_has_zero_mfe():
    mae, mfe = excursions(100, [99, 98], side=1)
    assert mae == pytest.approx(0.02)
    assert mfe == 0.0
