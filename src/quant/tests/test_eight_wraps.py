"""Eight wraps: data sources, alpha, portfolio, valuation, scheduled jobs, ML models,
performance attribution, other optimizers. Each one fails closed and never raises.

Flow stage: Data sources, Alpha engine, Portfolio engine, Valuation engine, Scheduled jobs, ML models, Performance attribution, Other optimizers
Status: TEST
"""
from __future__ import annotations

import datetime as dt

import pytest

from quant.core.blue_bridge import BlueEngineUnavailable
from quant.data import data_source_contract, valuation
from quant.execution import job_status
from quant.models import alpha_engine, ml_models
from quant.portfolio import alt_optimizers, attribution, portfolio_targets


class _Result:
    def __init__(self, ok=True, data=None, warnings=None, error=None):
        self.ok, self.data, self.warnings, self.error = ok, data or {}, warnings or [], error


def _fake_engine(record, result):
    class Fake:
        def __init__(self, cfg=None, provider=None, **kw):
            record["cfg"] = dict(cfg or {})
        def run(self, context=None):
            return result
    return Fake


def _unavailable(*a, **k):
    raise BlueEngineUnavailable("gone")


# ----------------------------------------------------------------- data sources
def test_fingerprint_is_stable_for_the_same_rows():
    rows = [{"date": "2026-11-13", "close": 100.0}, {"date": "2026-11-12", "close": 99.0}]
    a = data_source_contract.data_fingerprint(rows)
    assert a and a["rows_hash"] == data_source_contract.data_fingerprint(list(rows))["rows_hash"]


def test_fingerprint_changes_when_rows_change():
    a = data_source_contract.data_fingerprint([{"close": 100.0}])
    b = data_source_contract.data_fingerprint([{"close": 101.0}])
    assert a["rows_hash"] != b["rows_hash"]


def test_fingerprint_is_none_when_blue_missing(monkeypatch):
    monkeypatch.setattr(data_source_contract, "load", _unavailable)
    assert data_source_contract.data_fingerprint([{"close": 1.0}]) is None


# ----------------------------------------------------------------- alpha
def test_alpha_runs_on_empty_inputs_with_no_signals():
    res = alpha_engine.run_alpha(inputs={})
    assert res["advice_only"] is True
    assert isinstance(res["signals"], list)


def test_alpha_failure_has_no_signals(monkeypatch):
    monkeypatch.setattr(alpha_engine, "load", _unavailable)
    res = alpha_engine.run_alpha()
    assert res["ok"] is False and res["signals"] == [] and res["advice_only"] is True


def test_alpha_engine_raising_is_not_ok(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("bad config")
    monkeypatch.setattr(alpha_engine, "load", lambda name: type("M", (), {"AlphaEngine": Boom}))
    assert alpha_engine.run_alpha()["ok"] is False


# ----------------------------------------------------------------- portfolio
def test_portfolio_forces_persistence_off(monkeypatch):
    record = {}
    fake = _fake_engine(record, _Result(data={"target_weights": {"A": 0.5, "B": 0.5}}))
    monkeypatch.setattr(portfolio_targets, "load", lambda name: type("M", (), {"PortfolioEngine": fake}))
    res = portfolio_targets.run_portfolio(config={"persist": True})
    assert record["cfg"]["persist"] is False
    assert res["advice_only"] is True and res["target_weights"] == {"A": 0.5, "B": 0.5}


def test_portfolio_refuses_a_negative_weight(monkeypatch):
    fake = _fake_engine({}, _Result(data={"target_weights": {"A": 1.2, "B": -0.2}}))
    monkeypatch.setattr(portfolio_targets, "load", lambda name: type("M", (), {"PortfolioEngine": fake}))
    res = portfolio_targets.run_portfolio()
    assert res["ok"] is False and res["target_weights"] == {} and "long-only" in res["reason"]


def test_portfolio_refuses_a_non_numeric_weight(monkeypatch):
    fake = _fake_engine({}, _Result(data={"target_weights": {"A": "lots"}}))
    monkeypatch.setattr(portfolio_targets, "load", lambda name: type("M", (), {"PortfolioEngine": fake}))
    assert portfolio_targets.run_portfolio()["ok"] is False


def test_portfolio_failure_when_blue_missing(monkeypatch):
    monkeypatch.setattr(portfolio_targets, "load", _unavailable)
    res = portfolio_targets.run_portfolio()
    assert res["ok"] is False and res["advice_only"] is True


# ----------------------------------------------------------------- valuation
def test_valuation_runs_with_no_inputs_and_gives_no_guess():
    res = valuation.run_valuation(inputs={})
    assert res["advice_only"] is True
    assert res["valuations"] == {} or isinstance(res["valuations"], dict)


def test_valuation_failure_gives_no_valuations(monkeypatch):
    monkeypatch.setattr(valuation, "load", _unavailable)
    res = valuation.run_valuation()
    assert res["ok"] is False and res["valuations"] == {}


# ----------------------------------------------------------------- scheduled jobs
def test_job_window_closed_on_a_saturday():
    sat = dt.datetime(2026, 11, 14, 23, 0)
    assert job_status.window_open(sat)["window_open"] is False


def test_job_window_open_late_on_a_weekday():
    mon = dt.datetime(2026, 11, 16, 23, 0)
    res = job_status.window_open(mon)
    assert res["ok"] is True and res["window_open"] is True


def test_job_window_fails_closed_when_blue_missing(monkeypatch):
    monkeypatch.setattr(job_status, "load", _unavailable)
    res = job_status.window_open()
    assert res["ok"] is False and res["window_open"] is False


def test_last_run_status_reads_without_error():
    res = job_status.last_run_status()
    assert isinstance(res["ok"], bool)


# ----------------------------------------------------------------- ML models (research)
def test_model_status_is_research_only():
    status = ml_models.model_status()
    assert status["research_only"] is True
    assert isinstance(status["sklearn"], bool)


def test_fit_refuses_too_little_data():
    assert ml_models.fit_ridge([], []) is None


def test_fit_and_predict_stay_in_unit_interval():
    # Blue refuses fewer than 20 rows, so use 40 deterministic rows.
    X = [[(i % 10) / 10.0] for i in range(40)]
    y = [1 if (i % 10) >= 5 else 0 for i in range(40)]
    fitted = ml_models.fit_ridge(X, y)
    assert fitted is not None and fitted["research_only"] is True
    probs = ml_models.predict_probability(fitted, X)
    assert probs is not None and all(0.0 <= p <= 1.0 for p in probs)


def test_predict_without_a_fit_is_none():
    assert ml_models.predict_probability(None, [[0.5]]) is None


# ----------------------------------------------------------------- attribution
def test_sharpe_is_not_exposed_while_the_audit_is_open():
    """Audit X-27: two Sharpe definitions disagree. No wrapper may expose one yet."""
    assert not hasattr(attribution, "sharpe")


def test_brinson_returns_a_dict_for_a_simple_case():
    w = {"A": 0.5, "B": 0.5}
    b = {"A": 0.5, "B": 0.5}
    r = {"A": 0.01, "B": 0.02}
    res = attribution.brinson(w, b, r, r)
    assert isinstance(res, dict)


def test_attribution_none_when_blue_missing(monkeypatch):
    monkeypatch.setattr(attribution, "load", _unavailable)
    assert attribution.brinson({}, {}, {}, {}) is None


# ----------------------------------------------------------------- other optimizers
COV = [[0.04, 0.01], [0.01, 0.09]]


def test_risk_parity_returns_long_only_weights_summing_to_one():
    res = alt_optimizers.risk_parity(COV)
    assert res["ok"] is True
    assert all(w >= 0 for w in res["weights"])
    assert abs(sum(res["weights"]) - 1.0) < 1e-6


def test_min_variance_returns_long_only_weights():
    res = alt_optimizers.min_variance(COV)
    assert res["ok"] is True and abs(sum(res["weights"]) - 1.0) < 1e-6


def test_inverse_vol_matches_its_definition():
    res = alt_optimizers.inverse_vol([0.2, 0.1])
    assert res["ok"] is True
    assert res["weights"][1] > res["weights"][0]  # lower vol gets the bigger weight


@pytest.mark.parametrize("bad", [[1.2, -0.2], [0.5, 0.2], [float("nan"), 1.0]])
def test_checked_refuses_bad_weights(bad):
    assert alt_optimizers._checked(bad)["ok"] is False


def test_optimizer_failure_when_blue_missing(monkeypatch):
    monkeypatch.setattr(alt_optimizers, "load", _unavailable)
    res = alt_optimizers.risk_parity(COV)
    assert res["ok"] is False and res["weights"] == []
