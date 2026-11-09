"""Phase 1: costs, backtest (no look-ahead), walk-forward, validation statistics."""
import numpy as np
import pandas as pd
import pytest

from quant.research.backtest import run_backtest
from quant.research.costs import CostModel
from quant.research.validation import (
    bootstrap_mean_pvalue,
    deflated_sharpe,
    prob_backtest_overfit,
    sharpe,
)
from quant.research.walk_forward import purged_walk_forward
from quant.research.trial_log import TrialLog
from quant.research.capacity import max_capital


def test_delivery_stt_on_both_legs_and_stamp_on_buy_only():
    # Owner-confirmed rule: delivery STT applies to both legs; stamp duty to the buy leg.
    # Brokerage is zero on delivery, so the only sell-minus-buy gap is the stamp duty.
    cm = CostModel()
    buy = cm.trade_cost(100_000, "BUY", "delivery")
    sell = cm.trade_cost(100_000, "SELL", "delivery")
    assert sell - buy == pytest.approx(-cm.stamp_buy_delivery * 100_000)
    assert cm.trade_cost(1e9, "BUY") - cm.trade_cost(1e9 - 1, "BUY") > 0


def test_brokerage_is_capped():
    # Rs 100 crore intraday buy: brokerage 20 + txn 30700 + SEBI 1000 + GST 5709.6
    # + stamp 30000 + impact 500000 = 567429.6 (brokerage cap binds; 0.03% would be 3e5).
    cm = CostModel()
    assert cm.trade_cost(1e9, "BUY", "intraday") == pytest.approx(567429.6, rel=1e-9)


def _bars():
    dates = pd.bdate_range("2026-01-01", periods=6)
    rows = []
    for i, d in enumerate(dates):
        rows.append({"date": d, "symbol": "AAA", "open": 100 + i, "close": 101 + i})
    return pd.DataFrame(rows)


def test_target_set_at_close_fills_next_open_no_lookahead():
    bars = _bars()
    targets = pd.DataFrame([{"date": bars.date.iloc[1], "symbol": "AAA", "target_qty": 10}])
    res = run_backtest(bars, targets, capital=1e6)
    first = res.trades.iloc[0]
    assert first["date"] == bars.date.iloc[2]          # next bar after the signal
    assert first["price"] == bars.open.iloc[2]          # filled at that bar's open


def test_missing_bar_counts_as_missed_fill():
    aaa = _bars()
    bbb = aaa.assign(symbol="BBB")
    bars = pd.concat([aaa, bbb])
    # AAA has no bar on day 2, so its order set on day 1 cannot fill that day.
    bars = bars[~((bars.date == aaa.date.iloc[2]) & (bars.symbol == "AAA"))]
    targets = pd.DataFrame([{"date": aaa.date.iloc[1], "symbol": "AAA", "target_qty": 5}])
    res = run_backtest(bars, targets)
    assert res.missed_fills == 1
    # The target still holds, so the order fills on the next bar, not the missing one.
    assert list(res.trades.date) == [aaa.date.iloc[3]]


def test_purged_walk_forward_keeps_train_before_test_minus_purge():
    folds = list(purged_walk_forward(100, n_splits=4, purge=5))
    assert len(folds) == 4
    for train, test in folds:
        assert train.max() < test.min() - 5 + 1
        assert len(set(train) & set(test)) == 0


def test_sharpe_and_deflated_sharpe_ordering():
    rng = np.random.default_rng(1)
    strong = rng.normal(0.01, 0.01, 500)
    noise = rng.normal(0.0, 0.01, 500)
    assert sharpe(strong) > sharpe(noise)
    assert deflated_sharpe(strong, n_trials=1, trial_sharpe_var=0.0) > 0.99
    assert deflated_sharpe(noise, n_trials=200, trial_sharpe_var=0.01) < 0.99


def test_pbo_in_unit_interval_and_bootstrap_detects_edge():
    rng = np.random.default_rng(2)
    M = rng.normal(0, 0.01, size=(160, 12))
    pbo = prob_backtest_overfit(M, n_blocks=8)
    assert 0.0 <= pbo <= 1.0
    edge = rng.normal(0.02, 0.01, 200)
    assert bootstrap_mean_pvalue(edge, n_boot=2000) < 0.01
    assert bootstrap_mean_pvalue(rng.normal(0, 0.01, 200), n_boot=2000) > 0.05


def test_trial_log_counts_and_variance(tmp_path):
    log = TrialLog(tmp_path / "trials.jsonl")
    log.record("s1", {"lb": 10}, 0.5, 250)
    log.record("s1", {"lb": 20}, 1.5, 250)
    log.record("s2", {}, 0.0, 250)
    assert log.count() == 3 and log.count("s1") == 2
    assert log.sharpe_variance("s1") > 0


def test_capacity_formula():
    assert max_capital(1e8, 0.01, 2) == pytest.approx(2e6)
    with pytest.raises(ValueError):
        max_capital(-1, 0.01, 1)
