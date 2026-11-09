"""Dashboard numbers checked by hand on fixed trades, and the EV/MAE/MFE removal rule.

Flow stage: Dashboard, Live monitor
Status: TEST

Owner rule (decision 0008, 2026-11-05): EV, MAE/MFE and rule compliance are NOT on the
dashboard. The dashboard keeps P&L, win rate, profit factor and risk use. The
excursion helper (MAE/MFE per trade) is still tested here because the optimizer uses it.

Expected numbers are worked out by hand in the comments. Trade list (return, rupee P&L):
  T1 ( +2%, +2000)   T2 ( -1%, -1000)   T3 ( +3%, +3000)
  T4 ( -2%, -2000)   T5 ( +1%, +1000)
"""
from pathlib import Path

import pytest

from quant.monitor.live_monitor import excursions
from quant.reporting.dashboard import build_payload


#: Keys the dashboard must NOT show (decision 0008).
REMOVED_KEYS = ("ev_pct_per_trade", "avg_mae_pct", "avg_mfe_pct", "rule_breaches")


TRADES = [
    {"ret": 0.02, "pnl": 2000.0},
    {"ret": -0.01, "pnl": -1000.0},
    {"ret": 0.03, "pnl": 3000.0},
    {"ret": -0.02, "pnl": -2000.0},
    {"ret": 0.01, "pnl": 1000.0},
]


def test_fixed_trade_list_payload():
    p = build_payload(TRADES, risk_used=0.3)
    # Wins 3 of 5 trades
    assert p["win_rate"] == pytest.approx(0.6)
    # Gross profit 2000 + 3000 + 1000 = 6000; gross loss 1000 + 2000 = 3000 -> PF 2.0
    assert p["profit_factor"] == pytest.approx(2.0)
    # Net P&L 6000 - 3000 = 3000
    assert p["total_pnl"] == pytest.approx(3000.0)
    assert p["trades"] == 5
    assert p["risk_utilisation"] == 0.3


def test_removed_metrics_are_not_in_payload():
    # Decision 0008: EV, MAE/MFE and rule compliance are off the dashboard.
    p = build_payload(TRADES, risk_used=0.3)
    for key in REMOVED_KEYS:
        assert key not in p


def test_removed_metrics_stay_off_even_when_trades_carry_excursions():
    trades = [{"ret": 0.01, "pnl": 100.0, "mae": 0.01, "mfe": 0.02}]
    p = build_payload(trades, risk_used=0.0)
    for key in REMOVED_KEYS:
        assert key not in p


def test_break_even_trade_is_not_a_win_and_not_a_loss():
    trades = [{"ret": 0.0, "pnl": 0.0}, {"ret": 0.01, "pnl": 100.0}, {"ret": -0.01, "pnl": -50.0}]
    p = build_payload(trades, risk_used=0.0)
    # Win rate: 1 win of 3 trades
    assert p["win_rate"] == pytest.approx(1 / 3)
    # PF: 100 / 50 = 2.0 (the zero trade adds to neither side)
    assert p["profit_factor"] == pytest.approx(2.0)


def test_all_winners_has_no_profit_factor():
    trades = [{"ret": 0.01, "pnl": 100.0}, {"ret": 0.02, "pnl": 200.0}]
    p = build_payload(trades, risk_used=0.0)
    assert p["profit_factor"] is None  # no losses, so the ratio is undefined
    assert p["win_rate"] == pytest.approx(1.0)


def test_no_trades_reports_none_not_zero():
    p = build_payload([], risk_used=0.2)
    assert p["trades"] == 0
    assert p["win_rate"] is None
    assert p["profit_factor"] is None
    assert p["total_pnl"] == 0.0
    assert p["risk_utilisation"] == 0.2


# --- MAE / MFE per trade (still computed; they feed the optimizer) ---------------
# entry 100, path 98 -> 103 -> 96 -> 110 -> 105

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


def test_never_favourable_long_has_zero_mfe():
    mae, mfe = excursions(100, [99, 98], side=1)
    assert mae == pytest.approx(0.02)
    assert mfe == 0.0
