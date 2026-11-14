from __future__ import annotations

import json

import pytest

from blue_engine.backtest.costs import CostModel
from blue_engine.backtest.engine import BacktestConfig, run_backtest
from blue_engine.backtest.metrics import _round, compute_metrics
from blue_engine.ml.evaluation.metrics import deflated_sharpe


def _bars():
    return [
        {"ts": "2025-01-01", "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 10000},
        {"ts": "2025-01-02", "open": 110, "high": 113, "low": 109,
         "close": 112, "volume": 10000},
        {"ts": "2025-01-03", "open": 120, "high": 122, "low": 119,
         "close": 121, "volume": 10000},
        {"ts": "2025-01-04", "open": 130, "high": 132, "low": 129,
         "close": 131, "volume": 10000},
    ]


def _config(**kwargs):
    values = {
        "cost_model": CostModel(
            slippage_bps=5, latency_bps=0, impact_coefficient=0),
        "starting_equity": 100000,
    }
    values.update(kwargs)
    return BacktestConfig(**values)


def test_run_backtest_uses_prefix_signals_and_fills_at_next_bar_open():
    seen = []

    def signal(params, context, bars):
        seen.append((context["bar_index"], len(bars)))
        with pytest.raises(IndexError):
            bars[context["bar_index"] + 1]
        return 1.0 if context["bar_index"] == 0 else 0.0

    result = run_backtest(_bars(), signal, {}, config=_config())

    assert seen == [(0, 1), (1, 2), (2, 3)]
    assert len(result.equity) == len(_bars()) + 1
    assert result.equity[0] == 100000
    assert len(result.fills) == 2
    assert result.fills[0].ts == "2025-01-02"
    assert result.fills[0].reference_price == 110
    assert result.fills[0].price > result.fills[0].reference_price
    assert result.fills[1].ts == "2025-01-03"
    assert result.fills[1].side == "SELL"
    assert result.metrics["n_trades"] == 1
    assert result.metrics["costs_paid"] > 0
    assert result.envelope["bar_count"] == len(_bars())


def test_gap_down_is_reflected_at_next_open_not_previous_close():
    rows = [
        {"ts": "2025-01-01", "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 10000},
        {"ts": "2025-01-02", "open": 100, "high": 102, "low": 99,
         "close": 100, "volume": 10000},
        {"ts": "2025-01-03", "open": 100, "high": 101, "low": 98,
         "close": 99, "volume": 10000},
        {"ts": "2025-01-04", "open": 88, "high": 90, "low": 85,
         "close": 89, "volume": 10000},
    ]
    result = run_backtest(
        rows, lambda params, context, bars: 1.0 if context["bar_index"] < 2 else 0.0,
        {}, config=_config(cost_model=CostModel(
            slippage_bps=0, latency_bps=0, impact_coefficient=0)))

    assert result.fills[0].ts == "2025-01-02"
    assert result.fills[0].reference_price == 100
    assert result.fills[1].ts == "2025-01-04"
    assert result.fills[1].reference_price == 88
    assert result.trades[0]["exit_price"] == 88
    assert result.trades[0]["gross_pnl"] < 0
    assert result.equity[3] < result.equity[2]


def test_stop_loss_uses_adverse_first_bar_and_gap_fill():
    rows = [
        {"ts": "2025-01-01", "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 10000},
        {"ts": "2025-01-02", "open": 100, "high": 102, "low": 94,
         "close": 98, "volume": 10000},
        {"ts": "2025-01-03", "open": 95, "high": 97, "low": 94,
         "close": 96, "volume": 10000},
    ]
    result = run_backtest(
        rows, lambda p, c, b: 1.0, {},
        config=_config(
            cost_model=CostModel(slippage_bps=0, latency_bps=0,
                                 impact_coefficient=0),
            stop_loss_fraction=0.05))

    assert result.fills[-1].side == "SELL"
    assert result.fills[-1].reference_price == pytest.approx(95.0)
    assert result.trades[0]["exit_price"] == pytest.approx(95.0)
    assert any("stop loss triggered" in warning for warning in result.warnings)


def test_daily_loss_limit_liquidates_and_blocks_same_day_reentry():
    rows = [
        {"ts": "2025-01-01 09:15", "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 10000},
        {"ts": "2025-01-01 10:15", "open": 100, "high": 101, "low": 80,
         "close": 80, "volume": 10000},
        {"ts": "2025-01-01 11:15", "open": 80, "high": 81, "low": 79,
         "close": 80, "volume": 10000},
        {"ts": "2025-01-02 09:15", "open": 80, "high": 82, "low": 79,
         "close": 81, "volume": 10000},
    ]
    result = run_backtest(
        rows, lambda p, c, b: 1.0, {},
        config=_config(
            cost_model=CostModel(slippage_bps=0, latency_bps=0,
                                 impact_coefficient=0),
            max_daily_loss_fraction=0.10))

    assert result.fills[0].side == "BUY"
    assert result.fills[1].side == "SELL"
    assert result.fills[1].ts == "2025-01-01 11:15"
    assert not any(fill.side == "BUY" and fill.ts.startswith("2025-01-01 11")
                   for fill in result.fills)
    assert any("daily loss limit triggered" in warning for warning in result.warnings)


def test_run_backtest_marks_open_position_and_reports_costs():
    result = run_backtest(
        _bars(), lambda params, context, bars: 1.0, {},
        config=_config())

    assert len(result.fills) >= 1
    assert result.trades[-1]["status"] == "OPEN"
    assert result.trades[-1]["exit_ts"] is None
    assert result.metrics["n_trades"] == len(result.trades)
    assert result.equity[-1] > result.equity[0]
    json.dumps(result.to_dict(), allow_nan=False)


@pytest.mark.parametrize("target", [-0.2, 1.1, float("nan"), "bad"])
def test_run_backtest_rejects_invalid_or_unsafe_target_positions(target):
    with pytest.raises(ValueError):
        run_backtest(_bars(), lambda params, context, bars: target, {},
                     config=_config())


def test_run_backtest_can_enable_bounded_short_exposure_explicitly():
    result = run_backtest(
        _bars(), lambda params, context, bars: -0.5, {},
        config=_config(allow_short=True, max_position_fraction=0.5))

    assert result.fills[0].side == "SELL"
    assert result.fills[0].price < result.fills[0].reference_price


def test_run_backtest_rejects_insufficient_or_invalid_bars():
    with pytest.raises(ValueError, match="at least two"):
        run_backtest(_bars()[:1], lambda p, c, b: 0, {}, config=_config())
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        run_backtest(
            [{"ts": "2025-01-01", "open": 100, "high": 99, "low": 98,
              "close": 100, "volume": 1000},
             _bars()[1]],
            lambda p, c, b: 0, {}, config=_config())


def test_deflated_sharpe_single_trial_has_finite_expected_maximum():
    result = deflated_sharpe(0.01, n_trials=1, n_obs=100)

    assert result["expected_max_sharpe"] == 0.0
    assert result["prob_real"] is not None
    assert 0.0 <= result["prob_real"] <= 1.0

def test_metrics_keep_gross_profit_separate_from_costed_trade_pnl():
    metrics = compute_metrics(
        equity=[1000, 1001, 1002], trades=[
            {"gross_pnl": 10, "costs": 8, "pnl": 2},
            {"gross_pnl": -5, "costs": 1, "pnl": -6},
        ], total_bars=2, bars_in_market=2,
        starting_equity=1000)

    assert metrics["gross_profit"] == 10
    assert metrics["gross_loss"] == -5
    assert metrics["net_pnl"] == -4
    assert _round(float("inf")) is None
    assert _round(float("nan")) is None
