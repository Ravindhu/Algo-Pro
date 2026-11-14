from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

from blue_engine.backtest.batch import (
    BatchConfig, _cache_covers, _oos_gate, periods_per_year_for_interval,
    run_symbol_batch,
)
from blue_engine.optimizer.objective import Objective
from blue_engine.optimizer.data_cache import OptimizerDataCache
from blue_engine.optimizer.search_space import ParamSpec, SearchSpace


class _HistoryClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "success", "data": self.rows}


def _flat_signal(params, context, bars):
    return 0.0


def test_oos_gate_requires_absolute_relative_and_deflated_sharpe():
    base_is = {"sharpe": 2.0, "max_drawdown_pct": 10.0}
    base_oos = {
        "sharpe": 2.0, "max_drawdown_pct": 10.0,
        "deflated_prob_real": 0.99, "n_trials": 1, "n_bars": 250,
    }
    accepted = _oos_gate(base_is, base_oos, n_trials=1)
    overfit_rejected = _oos_gate(
        {"sharpe": 8.0, "max_drawdown_pct": 10.0}, base_oos, n_trials=1)
    multiple_test_rejected = _oos_gate(
        base_is, {**base_oos, "deflated_prob_real": 0.80, "n_trials": 100},
        n_trials=100)
    bad_drawdown = _oos_gate(
        base_is, {**base_oos, "max_drawdown_pct": 13.01}, n_trials=1)

    assert accepted["approved"] is True
    assert accepted["sharpe_floor"] == 1.4
    assert accepted["relative_sharpe_floor"] == 1.4
    assert accepted["absolute_sharpe_floor"] == 1.0
    assert overfit_rejected["approved"] is False
    assert multiple_test_rejected["dsr_pass"] is False
    assert multiple_test_rejected["n_trials"] == 100
    assert accepted["drawdown_ceiling_pct"] == 13.0
    assert bad_drawdown["approved"] is False

def test_oos_gate_rejects_trial_count_mismatch():
    import pytest
    with pytest.raises(ValueError, match="trial count mismatch"):
        _oos_gate(
            {"sharpe": 2.0, "max_drawdown_pct": 10.0},
            {"sharpe": 2.0, "max_drawdown_pct": 10.0,
             "deflated_prob_real": 0.99, "n_trials": 1},
            n_trials=10)

def test_oos_gate_rejects_invalid_trial_count():
    import pytest
    with pytest.raises(ValueError, match="n_trials"):
        _oos_gate(
            {"sharpe": 2.0, "max_drawdown_pct": 10.0},
            {"sharpe": 2.0, "max_drawdown_pct": 10.0,
             "deflated_prob_real": 0.99, "n_trials": 1},
            n_trials=0)


def test_intraday_period_annualization_uses_session_length():
    assert periods_per_year_for_interval("15m") == 6300
    assert periods_per_year_for_interval("1h") == 1575


def test_optimizer_applies_configured_period_risk_free_rate():
    def positive_returns(params, context):
        return {
            "ok": True,
            "returns": [0.01 + (0.002 if i % 2 else -0.002)
                        for i in range(30)],
        }

    without_risk_free = Objective(
        positive_returns, periods=60, n_folds=2).evaluate({})
    with_risk_free = Objective(
        positive_returns, periods=60, n_folds=2,
        risk_free_per_period=0.01).evaluate({})

    assert without_risk_free.ok
    assert with_risk_free.ok
    assert without_risk_free.mean_sharpe > with_risk_free.mean_sharpe


def test_cache_window_coverage_accepts_weekend_and_holiday_edges():
    status = SimpleNamespace(
        cached=True, first_ts="2025-01-06 09:15:00",
        last_ts="2025-01-09 15:30:00")

    assert _cache_covers(status, "2025-01-04", "2025-01-10")
    assert not _cache_covers(status, "2024-12-01", "2025-01-10")


def test_symbol_batch_caches_before_search_and_never_promotes_failed_search(
        monkeypatch, tmp_path):
    start = date(2025, 1, 1)
    rows = []
    for index in range(120):
        ts = (start + timedelta(days=index)).isoformat()
        price = 100.0 + (index % 7) * 0.2
        rows.append({
            "timestamp": ts, "open": price, "high": price + 1,
            "low": price - 1, "close": price, "volume": 10000,
        })
    client = _HistoryClient(rows)
    from blue_engine.portfolio.providers import factory
    monkeypatch.setattr(factory, "get_client", lambda: client)

    cache = OptimizerDataCache(root=tmp_path / "cache")
    space = SearchSpace(
        name="batch_test",
        params=[ParamSpec(name="period", kind="int", low=2, high=5,
                          default=3)])
    output = tmp_path / "output" / "symbol_parameters.json"
    batch = run_symbol_batch(
        ["TEST"], strategy=_flat_signal, search_space=space, cache=cache,
        config=BatchConfig(
            start=start.isoformat(),
            end=(start + timedelta(days=119)).isoformat(),
            trials=2, folds=2, workers=1, download_workers=1),
        output_path=output)

    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert len(client.calls) == 1
    assert cache.status(
        exchange="NSE", symbol="TEST", interval="1d").n_bars == 120
    assert batch.configurations == {}
    assert persisted["configurations"] == {}
    assert persisted["results"]["TEST"]["state"] == "DEGRADED"
    assert "recommendation" in persisted["results"]["TEST"]["reason"]
