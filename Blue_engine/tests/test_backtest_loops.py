from __future__ import annotations

import pytest

from blue_engine.backtest.costs import CostModel
from blue_engine.backtest.engine import BacktestConfig
from blue_engine.backtest.loops import LiveMonitor, SlippageReconciler


def test_live_monitor_emits_pause_signal_to_subscribers():
    monitor = LiveMonitor({
        "max_dd": 10.0,
        "max_losing_streak": 5,
    }, baseline_source="verified_live")
    received = []
    monitor.subscribe(received.append)

    signal = monitor.evaluate(
        live_equity=[100.0, 80.0],
        live_trade_pnls=[-1.0] * 8)

    assert signal.triggered is True
    assert signal.state == "RE_OPTIMIZING"
    assert signal.pause_new_entries is True
    assert signal.risk_scale == 0
    assert len(signal.reasons) == 2
    assert received == [signal]


def test_live_monitor_does_not_trigger_inside_historical_envelope():
    monitor = LiveMonitor({
        "max_dd": 10.0,
        "max_losing_streak": 5,
    }, baseline_source="live")
    signal = monitor.evaluate(
        live_equity=[100.0, 96.0],
        live_trade_pnls=[-1.0, 2.0],
        live_daily_returns=[0.01] * 10,
        historical_daily_returns=[0.01] * 30)

    assert signal.triggered is False
    assert signal.state == "LIVE"
    assert signal.pause_new_entries is False
    assert signal.risk_scale == 1


def test_live_monitor_rejects_unverified_backtest_baseline():
    with pytest.raises(ValueError, match="baseline_source"):
        LiveMonitor({"max_dd": 10.0, "max_losing_streak": 5})


def test_slippage_reconciliation_calibrates_copy_and_retests():
    cost_model = CostModel(slippage_bps=5, latency_bps=0,
                           impact_coefficient=0)
    reconciler = SlippageReconciler(threshold_bps=1)
    rows = [
        {"ts": "2025-01-01", "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 10000},
        {"ts": "2025-01-02", "open": 101, "high": 102, "low": 100,
         "close": 101, "volume": 10000},
        {"ts": "2025-01-03", "open": 102, "high": 103, "low": 101,
         "close": 102, "volume": 10000},
    ]
    config = BacktestConfig(cost_model=cost_model)
    result = reconciler.reconcile_and_retest(
        cost_model=cost_model,
        matched_fills=[{
            "fill_id": "fill-1", "side": "BUY", "reference_price": 100,
            "backtest_price": 100.1, "live_price": 100.3,
        }],
        rows=rows, strategy=lambda params, context, bars: 1.0,
        params={}, config=config)

    assert result.triggered is True
    assert result.mean_slippage_delta_bps == pytest.approx(20.0)
    assert result.previous_slippage_bps == 5
    assert result.calibrated_slippage_bps == pytest.approx(25.0)
    assert cost_model.slippage_bps == 5
    assert result.cost_model.slippage_bps == pytest.approx(25.0)
    assert result.backtest is not None


def test_slippage_reconciliation_rejects_ambiguous_or_empty_matches():
    reconciler = SlippageReconciler()
    cost_model = CostModel()
    with pytest.raises(ValueError, match="unique non-empty"):
        reconciler.reconcile(
            cost_model=cost_model,
            matched_fills=[{"side": "BUY"}, {"side": "BUY"}])
    with pytest.raises(ValueError, match="no matched BUY"):
        reconciler.reconcile(
            cost_model=cost_model,
            matched_fills=[{
                "fill_id": "fill-1", "side": "SELL", "reference_price": 100,
                "backtest_price": 99, "live_price": 98,
            }])
