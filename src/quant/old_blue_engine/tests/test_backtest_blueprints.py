"""Tests for the OCL blueprint walk-forward comparison (backtest/blueprints.py).

What is under test is the COMPARISON, not the optimizer: the per-blueprint
batch is injected, so these tests exercise the pooling arithmetic, the
verdict gates, the shared-universe exclusion rule and the persistence - the
parts where a bug would silently change which blueprint gets recommended.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from blue_engine.backtest import blueprints as bp
from blue_engine.backtest.blueprints import (
    BlueprintConfig, blueprint_space, compare_blueprints, decide,
    pooled_metrics,
)


# --------------------------------------------------------------------------- #
# fixtures: a fake batch runner
# --------------------------------------------------------------------------- #


@dataclass
class _FakeBatch:
    results: dict


def _outcome(symbol, *, pnl, n_trades, sharpe, dsr=0.99, state="APPROVED"):
    return {
        "symbol": symbol, "state": state,
        "oos_metrics": {
            "n_trades": n_trades, "n_wins": max(1, n_trades // 2),
            "net_pnl": pnl, "gross_profit": pnl * 1.5 if pnl > 0 else 0.0,
            "gross_loss": -abs(pnl) * 0.5 if pnl > 0 else -abs(pnl),
            "costs_paid": abs(pnl) * 0.1, "sharpe": sharpe,
            "deflated_prob_real": dsr, "max_drawdown_pct": 8.0,
            "starting_equity": 100000.0, "exposure_pct": 0.5,
        },
        "is_metrics": {"sharpe": sharpe + 0.5, "max_drawdown_pct": 6.0},
        "best_parameters": {"setup_mode": "pinned"},
        "oos_window": ["2025-01-01", "2025-03-31"],
    }


def _runner(table, seen=None):
    """A batch runner that answers per (symbol, pinned setup_mode)."""
    def _run(symbols, *, strategy, search_space, config, cache, cost_model,
             output_path):
        mode = search_space.defaults()["setup_mode"]
        if seen is not None:
            seen.append({"mode": mode, "symbols": list(symbols),
                         "space": search_space, "cost": cost_model,
                         "output": output_path})
        results = {}
        for symbol in symbols:
            spec = table.get((mode, symbol))
            if spec is None:
                results[symbol] = {"symbol": symbol, "state": "DEGRADED",
                                   "reason": "no cached data"}
            else:
                results[symbol] = _outcome(symbol, **spec)
        return _FakeBatch(results=results)
    return _run


# --------------------------------------------------------------------------- #
# the pinned space
# --------------------------------------------------------------------------- #


def test_blueprint_space_pins_setup_mode_and_nothing_else():
    space = blueprint_space(mode="SELL_FAILURE_BOTTOM")
    assert space.n_params == 1
    assert space.names == ["setup_mode"]
    assert space.defaults() == {"setup_mode": "SELL_FAILURE_BOTTOM"}
    assert space.validate(space.defaults())[0] is True


def test_blueprint_space_rejects_an_empty_mode():
    with pytest.raises(ValueError):
        blueprint_space(mode="  ")


def test_default_setup_modes_are_the_three_blueprints():
    assert set(bp.DEFAULT_SETUP_MODES) == {
        "OCL", "SELL_FAILURE_TOP", "SELL_FAILURE_BOTTOM"}


# --------------------------------------------------------------------------- #
# pooling
# --------------------------------------------------------------------------- #


def test_expectancy_is_trade_weighted_not_a_mean_of_means():
    """A 40-trade symbol must dominate a 2-trade symbol, not tie with it."""
    pooled = pooled_metrics([
        {"symbol": "A", "state": "APPROVED",
         "oos_metrics": {"n_trades": 40, "net_pnl": 400.0, "n_wins": 25,
                         "sharpe": 2.0}},
        {"symbol": "B", "state": "APPROVED",
         "oos_metrics": {"n_trades": 2, "net_pnl": -100.0, "n_wins": 0,
                         "sharpe": -2.0}},
    ])
    assert pooled["n_trades"] == 42
    assert pooled["net_pnl"] == 300.0
    # 300 / 42 = 7.142857..., NOT the 50.0 a mean-of-means would report.
    assert pooled["expectancy"] == pytest.approx(7.142857, abs=1e-6)
    # Median of [2.0, -2.0] is 0.0 - the lucky name cannot carry the Sharpe.
    assert pooled["sharpe"] == 0.0
    assert pooled["deflated_prob_real"] is None      # absent, not invented


def test_pooled_metrics_of_nothing_returns_none_not_zero():
    pooled = pooled_metrics([])
    assert pooled["n_trades"] == 0
    assert pooled["expectancy"] is None
    assert pooled["sharpe"] is None


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #


def _row(mode, *, expectancy, sharpe, n_trades=30, dsr=0.99, approved=True):
    return {"setup_mode": mode, "state": "SCORED", "all_symbols_approved": approved,
            "pooled": {"expectancy": expectancy, "sharpe": sharpe,
                       "n_trades": n_trades, "deflated_prob_real": dsr}}


def test_decide_recommends_the_clear_leader():
    verdict = decide([_row("OCL", expectancy=10, sharpe=1.8),
                      _row("SELL_FAILURE_TOP", expectancy=2, sharpe=1.2),
                      _row("SELL_FAILURE_BOTTOM", expectancy=-5, sharpe=0.3)])
    assert verdict["decision"] == "RECOMMENDED"
    assert verdict["recommended_setup_mode"] == "OCL"


def test_decide_returns_tie_inside_the_margin():
    verdict = decide([_row("OCL", expectancy=10, sharpe=1.60),
                      _row("SELL_FAILURE_TOP", expectancy=9, sharpe=1.50)])
    assert verdict["decision"] == "TIE"
    assert verdict["recommended_setup_mode"] is None


def test_decide_refuses_a_negative_expectancy_even_with_a_high_sharpe():
    verdict = decide([_row("OCL", expectancy=-1.0, sharpe=2.5)])
    assert verdict["decision"] == "NO BLUEPRINT ELIGIBLE"
    assert "positive_expectancy" in verdict["ranking"][0]["failed_gates"]


def test_decide_requires_the_absolute_sharpe_and_the_sample():
    thin = decide([_row("OCL", expectancy=50, sharpe=3.0, n_trades=3)])
    assert thin["decision"] == "NO BLUEPRINT ELIGIBLE"
    assert "sample" in thin["ranking"][0]["failed_gates"]
    weak = decide([_row("OCL", expectancy=50, sharpe=0.5)])
    assert "absolute_sharpe" in weak["ranking"][0]["failed_gates"]


def test_decide_requires_the_deflated_probability_and_every_symbol():
    overfit = decide([_row("OCL", expectancy=50, sharpe=3.0, dsr=0.55)])
    assert overfit["decision"] == "NO BLUEPRINT ELIGIBLE"
    assert "deflated_sharpe" in overfit["ranking"][0]["failed_gates"]
    partial = decide([_row("OCL", expectancy=50, sharpe=3.0, approved=False)])
    assert "all_symbols_approved" in partial["ranking"][0]["failed_gates"]


def test_decide_never_raises_on_a_missing_number():
    verdict = decide([{"setup_mode": "OCL", "pooled": {}, "state": "FAILED"}])
    assert verdict["decision"] == "NO BLUEPRINT ELIGIBLE"


# --------------------------------------------------------------------------- #
# the comparison
# --------------------------------------------------------------------------- #


def _cfg(tmp_path, symbols=("INFY", "TCS")):
    return BlueprintConfig(
        symbols=list(symbols), start="2025-01-01", end="2025-12-31",
        setup_modes=("OCL", "SELL_FAILURE_TOP", "SELL_FAILURE_BOTTOM"),
        trials=5, folds=2, output_path=tmp_path / "comparison.json")


def test_comparison_runs_every_blueprint_on_the_same_universe(tmp_path):
    seen = []
    sharpes = {"OCL": 2.4, "SELL_FAILURE_TOP": 1.6, "SELL_FAILURE_BOTTOM": 1.0}
    table = {(mode, sym): {"pnl": 100.0, "n_trades": 20,
                          "sharpe": sharpes[mode]}
             for mode in sharpes for sym in ("INFY", "TCS")}
    result = compare_blueprints(
        _cfg(tmp_path), strategy="fake", batch_runner=_runner(table, seen))

    assert [entry["mode"] for entry in seen] == [
        "OCL", "SELL_FAILURE_TOP", "SELL_FAILURE_BOTTOM"]
    assert all(entry["symbols"] == ["INFY", "TCS"] for entry in seen)
    assert len({id(entry["cost"]) for entry in seen}) == 1, \
        "one shared cost model - costs must not differ between blueprints"
    assert result.verdict["decision"] == "RECOMMENDED"
    assert result.verdict["recommended_setup_mode"] == "OCL"
    # Each blueprint persists its own candidate file; none overwrites another.
    assert len({str(entry["output"]) for entry in seen}) == 3


def test_comparison_excludes_a_failed_symbol_from_every_blueprint(tmp_path):
    table = {}
    for mode in ("OCL", "SELL_FAILURE_TOP", "SELL_FAILURE_BOTTOM"):
        table[(mode, "INFY")] = {"pnl": 100.0, "n_trades": 20, "sharpe": 1.8}
        if mode != "SELL_FAILURE_TOP":
            # TCS only scores for two of the three blueprints.
            table[(mode, "TCS")] = {"pnl": 50.0, "n_trades": 10, "sharpe": 1.5}
    result = compare_blueprints(
        _cfg(tmp_path), strategy="fake", batch_runner=_runner(table))

    for row in result.rows:
        assert set(row["per_symbol"]) == {"INFY"}, \
            "a symbol missing for one blueprint must be dropped for all"
        assert row["excluded_symbols"] == {"TCS": "no cached data"}
        assert row["pooled"]["n_trades"] == 20


def test_comparison_reports_a_failed_blueprint_as_ineligible(tmp_path):
    table = {(mode, sym): {"pnl": 100.0, "n_trades": 20, "sharpe": 1.8}
             for mode in ("OCL", "SELL_FAILURE_BOTTOM") for sym in ("INFY", "TCS")}

    def _boom(symbols, **kwargs):
        if kwargs["search_space"].defaults()["setup_mode"] == "SELL_FAILURE_TOP":
            raise RuntimeError("study crashed")
        return _runner(table)(symbols, **kwargs)

    result = compare_blueprints(_cfg(tmp_path), strategy="fake", batch_runner=_boom)
    failed = [r for r in result.rows if r["setup_mode"] == "SELL_FAILURE_TOP"]
    assert failed and failed[0]["state"] == "FAILED"
    ranking = {r["setup_mode"]: r for r in result.verdict["ranking"]}
    assert ranking["SELL_FAILURE_TOP"]["eligible"] is False


def test_comparison_json_round_trips_and_never_claims_to_have_enabled(tmp_path):
    sharpes = {"OCL": 2.4, "SELL_FAILURE_TOP": 1.6, "SELL_FAILURE_BOTTOM": 1.0}
    table = {(mode, sym): {"pnl": 100.0, "n_trades": 20,
                          "sharpe": sharpes[mode]}
             for mode in sharpes for sym in ("INFY", "TCS")}
    cfg = _cfg(tmp_path)
    result = compare_blueprints(cfg, strategy="fake", batch_runner=_runner(table))

    payload = json.loads((tmp_path / "comparison.json").read_text("utf-8"))
    assert payload["enabled_for_paper"] is False
    assert payload["data_class"] == "historical:openalgo"
    assert len(payload["rows"]) == 3
    assert payload["verdict"]["recommended_setup_mode"] == "OCL"
    md = bp.comparison_markdown(result)
    assert "Decision: RECOMMENDED" in md
    assert "paper enable is a manual action" in md


def test_config_rejects_an_empty_universe_and_dedupes_modes():
    with pytest.raises(ValueError):
        BlueprintConfig(symbols=[], start="2025-01-01", end="2025-02-01")
    cfg = BlueprintConfig(symbols=["infy", "INFY"], start="2025-01-01",
                          end="2025-02-01",
                          setup_modes=["ocl", "OCL", "sell_failure_top"])
    assert cfg.symbols == ("INFY",)
    assert cfg.setup_modes == ("OCL", "SELL_FAILURE_TOP")
