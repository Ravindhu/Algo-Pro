"""Tests for expected value (EV) and sandbox statistics on the dashboard.

Flow stage: Dashboard
Status: IMPLEMENTED (paper data only)
"""
from __future__ import annotations

import pytest

from quant.reporting.dashboard import build_payload, expectancy
from quant.reporting.dashboard_data import build_overview


def test_ev_equals_win_rate_formula():
    # EV must equal p*avg_win + (1-p)*avg_loss.
    trades = [{"ret": 0.02, "pnl": 2000.0}, {"ret": 0.04, "pnl": 4000.0},
              {"ret": -0.01, "pnl": -1000.0}]
    out = expectancy(trades)
    p = 2 / 3
    assert out["ev_per_trade"] == pytest.approx(p * 3000.0 + (1 - p) * -1000.0)
    assert out["avg_win"] == pytest.approx(3000.0)
    assert out["avg_loss"] == pytest.approx(-1000.0)
    assert out["risk_reward"] == pytest.approx(3.0)
    assert out["ev_pct_per_trade"] == pytest.approx((0.02 + 0.04 - 0.01) / 3)


def test_no_trades_gives_none_not_zero():
    out = expectancy([])
    assert all(v is None for v in out.values())
    payload = build_payload([], risk_used=0.0)
    assert payload["ev_per_trade"] is None and payload["risk_reward"] is None


def test_only_wins_has_no_risk_reward():
    out = expectancy([{"ret": 0.01, "pnl": 100.0}])
    assert out["avg_loss"] is None
    assert out["risk_reward"] is None
    assert out["ev_per_trade"] == pytest.approx(100.0)


def test_overview_carries_ev_fields_for_dashboard():
    fills = [
        {"symbol": "AAA", "signed_qty": 10, "price": 100.0},
        {"symbol": "AAA", "signed_qty": -10, "price": 110.0},
        {"symbol": "BBB", "signed_qty": 5, "price": 200.0},
        {"symbol": "BBB", "signed_qty": -5, "price": 190.0},
    ]
    o = build_overview(fills=fills, audit_records=[], kill={}, limits={"blocked": False},
                       last_prices={})
    perf = o["performance"]
    assert perf["trades"] == 2
    assert perf["ev_per_trade"] == pytest.approx((100.0 + -50.0) / 2)
    assert perf["avg_win"] == pytest.approx(100.0)
    assert perf["avg_loss"] == pytest.approx(-50.0)
    assert perf["risk_reward"] == pytest.approx(2.0)
