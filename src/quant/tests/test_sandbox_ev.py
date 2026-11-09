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


def test_closed_trades_newest_first_with_pnl():
    fills = [
        {"symbol": "AAA", "signed_qty": 10, "price": 100.0},
        {"symbol": "AAA", "signed_qty": -10, "price": 110.0},
        {"symbol": "BBB", "signed_qty": 5, "price": 200.0},
        {"symbol": "BBB", "signed_qty": -5, "price": 190.0},
    ]
    o = build_overview(fills=fills, audit_records=[], kill={}, limits={"blocked": False},
                       last_prices={})
    rows = o["closed_trades"]
    assert [r["symbol"] for r in rows] == ["BBB", "AAA"]  # BBB closed last
    assert rows[0]["pnl"] == pytest.approx(-50.0)
    assert rows[1]["pnl"] == pytest.approx(100.0)


def test_closed_trades_newest_first_and_capped():
    fills = []
    for i in range(25):  # 25 round trips, each buys 1 at 100 and sells at 101
        fills += [{"symbol": f"S{i}", "signed_qty": 1, "price": 100.0},
                  {"symbol": f"S{i}", "signed_qty": -1, "price": 101.0}]
    o = build_overview(fills=fills, audit_records=[], kill={}, limits={"blocked": False},
                       last_prices={})
    assert len(o["closed_trades"]) == 20
    assert o["closed_trades"][0]["symbol"] == "S24"  # newest first
    assert o["closed_trades"][0]["pnl"] == pytest.approx(1.0)


def test_sandbox_equity_is_capital_plus_pnl():
    # Capital 100000; realized +100 (10 x 10); open long 10 @ 100 marked 105 = +50.
    fills = [{"symbol": "A", "signed_qty": 10, "price": 100.0},
             {"symbol": "A", "signed_qty": -10, "price": 110.0},
             {"symbol": "B", "signed_qty": 10, "price": 100.0}]
    o = build_overview(fills=fills, audit_records=[], kill={},
                       limits={"blocked": False, "capital_inr": 100000.0},
                       last_prices={"B": 105.0})
    sb = o["sandbox"]
    assert sb["capital_inr"] == pytest.approx(100000.0)
    assert sb["equity"] == pytest.approx(100150.0)
    assert sb["return_pct"] == pytest.approx(0.15)


def test_sandbox_equity_none_without_capital():
    o = build_overview(fills=[], audit_records=[], kill={}, limits={"blocked": False,
                       "capital_inr": None}, last_prices={})
    assert o["sandbox"] == {"capital_inr": None, "equity": None, "return_pct": None}


def test_fills_recent_newest_first_with_side():
    fills = [{"symbol": "A", "signed_qty": 5, "price": 100.0, "ts": "t1"},
             {"symbol": "A", "signed_qty": -5, "price": 102.0, "ts": "t2"}]
    o = build_overview(fills=fills, audit_records=[], kill={}, limits={"blocked": False},
                       last_prices={})
    assert o["fills_recent"][0] == {"symbol": "A", "side": "SELL", "qty": 5, "price": 102.0, "ts": "t2"}
    assert o["fills_recent"][1]["side"] == "BUY"


def test_closed_trade_has_return_percent():
    fills = [{"symbol": "A", "signed_qty": 10, "price": 100.0},
             {"symbol": "A", "signed_qty": -10, "price": 110.0}]
    o = build_overview(fills=fills, audit_records=[], kill={}, limits={"blocked": False},
                       last_prices={})
    assert o["closed_trades"][0]["ret_pct"] == pytest.approx(10.0)


def test_positions_carry_last_price():
    fills = [{"symbol": "B", "signed_qty": 10, "price": 100.0}]
    o = build_overview(fills=fills, audit_records=[], kill={}, limits={"blocked": False},
                       last_prices={"B": 105.0})
    assert o["positions"][0]["last_price"] == pytest.approx(105.0)
    assert o["positions"][0]["unrealized_pnl"] == pytest.approx(50.0)


def test_statutory_cost_one_buy_hand_checked():
    # BUY 10 @ 100 = notional 1000, delivery rates (bps): STT 10, txn 0.307, SEBI 0.01,
    # GST 18% of (txn + SEBI) = 0.0571, stamp 1.5, impact 5 -> total 16.874 bps -> Rs 1.6874
    from quant.research.costs import CostModel
    assert CostModel().trade_cost(1000.0, "BUY", "delivery") == pytest.approx(1.6874, rel=1e-3)


def test_overview_net_pnl_is_gross_minus_costs():
    fills = [{"symbol": "B", "signed_qty": 10, "price": 100.0},
             {"symbol": "B", "signed_qty": -10, "price": 110.0}]
    o = build_overview(fills=fills, audit_records=[], kill={}, limits={"blocked": False},
                       last_prices={})
    assert o["performance"]["realized_pnl"] == pytest.approx(100.0)
    assert o["costs"]["statutory_inr"] > 0
    assert o["costs"]["net_pnl"] == pytest.approx(100.0 - o["costs"]["statutory_inr"], abs=0.01)
