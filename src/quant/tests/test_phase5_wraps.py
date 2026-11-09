"""Phase 5: the seven Blue_engine modules wrapped instead of re-implemented.

Flow stage: Liquidity screener, India flow data, Algo router, Event scoring,
Walk-forward + purged CV, Historical bars, Champion/challenger, Signal
Status: TEST

One wrapper per section. Every section checks the PASS path and at least one
BLOCK path, because the block path is the one that protects capital:
  * screener   - a bad universe row is dropped, never passed through
  * economic   - the multiplier is clamped to [0.5, 1.0]; a stale feed cannot lever up
  * pov        - the wrapper cannot send an order, even when a client is supplied
  * agent team - a trade permission in a report is never echoed back
  * optimizer  - an unlogged run is refused; a study is read-only
  * backtest   - a strategy with no target position is an error, not a flat run
  * auto-tune  - an "apply" request is refused; the OOS policy is recomputed
  * strategies - a live signal CLASS cannot be scored as a return function

Sections marked Blue-dependent are skipped when Blue cannot load.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.core.blue_bridge import BlueEngineUnavailable, load

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _blue_ok(module: str) -> bool:
    try:
        load(module)
        return True
    except BlueEngineUnavailable:
        return False


BLUE_SCREENER = pytest.mark.skipif(not _blue_ok("screener.screener_engine"),
                                   reason="Blue screener unavailable")
BLUE_ECON = pytest.mark.skipif(not _blue_ok("economics.economic_engine"),
                               reason="Blue economic engine unavailable")
BLUE_POV = pytest.mark.skipif(not _blue_ok("pov.pov_engine"), reason="Blue POV unavailable")
BLUE_TEAM = pytest.mark.skipif(not _blue_ok("core.agent_team"), reason="Blue agent team unavailable")
BLUE_OPT = pytest.mark.skipif(not _blue_ok("optimizer.engine"), reason="Blue optimiser unavailable")
BLUE_BT = pytest.mark.skipif(not _blue_ok("backtest.engine"), reason="Blue backtest unavailable")
BLUE_TUNE = pytest.mark.skipif(not _blue_ok("core.auto_tune"), reason="Blue auto_tune unavailable")
BLUE_ALPHA = pytest.mark.skipif(not _blue_ok("alpha.registry"), reason="Blue alpha registry unavailable")


# --------------------------------------------------------------------------- #
# 1. SCREENER
# --------------------------------------------------------------------------- #
def test_universe_drops_junk_duplicates_and_blacklist():
    from quant.data.screener_engine import select_universe

    payload = {"selected": ["aapl", "AAPL", "msft", 3, "", None, " TSLA "],
               "asset_blacklist": ["msft"]}
    assert select_universe(payload) == ["AAPL", "TSLA"]


def test_universe_falls_back_to_symbols_then_to_nothing():
    from quant.data.screener_engine import select_universe

    assert select_universe({"symbols": ["aaaa", "bbbb"]}) == ["AAAA", "BBBB"]
    assert select_universe({"selected": "AAPL"}) == []        # wrong type: no universe
    assert select_universe({}) == []


def test_universe_cap_is_applied_last():
    from quant.data.screener_engine import select_universe

    payload = {"selected": ["A", "B", "C", "D"]}
    assert select_universe(payload, max_symbols=2) == ["A", "B"]
    with pytest.raises(ValueError):
        select_universe(payload, max_symbols=-1)


@BLUE_SCREENER
def test_screener_engine_runs_offline_through_a_provider():
    from quant.data.screener_engine import fetch_live, select_universe

    result = fetch_live(provider=lambda: {"candidates": [
        {"symbol": "AAPL", "metrics": {"market_cap": 1e12}},
        {"symbol": "MSFT", "metrics": {"market_cap": 1e12}}]})
    assert result["ok"] is True
    assert result["data"]["counts"]["candidates"] == 2
    assert select_universe(result["data"]) == result["data"]["symbols"]


# --------------------------------------------------------------------------- #
# 2. ECONOMIC
# --------------------------------------------------------------------------- #
def test_economic_multiplier_is_clamped_into_the_advice_band():
    from quant.data.economic_engine import risk_multiplier

    up = risk_multiplier({"data": {"gross_exposure_multiplier": 1.25, "regime": "RISK_ON"}})
    assert up["multiplier"] == 1.0 and up["clamped"] is True
    down = risk_multiplier({"data": {"gross_exposure_multiplier": 0.2, "regime": "RISK_OFF"}})
    assert down["multiplier"] == 0.5 and down["clamped"] is True


def test_economic_multiplier_fails_closed_on_bad_payloads():
    from quant.data.economic_engine import MIN_MULTIPLIER, risk_multiplier

    assert risk_multiplier({})["multiplier"] == MIN_MULTIPLIER
    assert risk_multiplier({"data": {"gross_exposure_multiplier": "lots"}})["multiplier"] == MIN_MULTIPLIER
    assert risk_multiplier({"data": {"gross_exposure_multiplier": float("nan")}})["multiplier"] == MIN_MULTIPLIER
    # A stale feed may not lever up, even when the engine emitted a high number.
    stale = risk_multiplier({"data": {"gross_exposure_multiplier": 1.3,
                                      "source_meta": {"stale": True}}})
    assert stale["multiplier"] == 1.0 and stale["capped_for_stale"] is True


@BLUE_ECON
def test_economic_engine_reads_a_macro_block():
    from quant.data.economic_engine import risk_multiplier, run_economic

    result = run_economic(macro={"gdp_growth": {"value": 7.0, "previous": 6.5},
                                 "inflation": {"value": 6.5, "previous": 4.0}})
    assert result["ok"] is True
    assert result["data"]["regime"] in {"RISK_OFF", "MILD_RISK_OFF", "NEUTRAL",
                                        "MILD_RISK_ON", "RISK_ON", "UNKNOWN"}
    assert 0.5 <= risk_multiplier(result)["multiplier"] <= 1.0


# --------------------------------------------------------------------------- #
# 3. POV
# --------------------------------------------------------------------------- #
def test_pov_rate_is_clamped_and_percent_form_understood():
    from quant.execution.pov_engine import MAX_POV, MIN_POV, clamp_pov, pov_rate

    assert pov_rate({"pov": {"participation_pct": 10}}) == pytest.approx(0.10)
    assert clamp_pov({"participation_pct": 0.001})[0] == MIN_POV
    assert clamp_pov({"participation_pct": 0.9})[0] == MAX_POV
    assert pov_rate({"pov": {"participation_pct": "ten"}}) == 0.1   # default, not a crash


def test_child_quantities_never_lose_or_duplicate_a_share():
    from quant.execution.pov_engine import child_quantities

    assert sum(child_quantities(100, [10, 10, 10], 0.1)) == 100   # remainder in the tail
    assert child_quantities(0, [100], 0.1) == []
    with pytest.raises(ValueError):
        child_quantities(-1, [100], 0.1)


@BLUE_POV
def test_pov_plan_never_sends_even_when_a_client_is_passed():
    from quant.execution.pov_engine import plan_pov

    plan = plan_pov({"pov": {"participation_pct": 10, "quantity": 500, "symbol": "INFY",
                             "action": "BUY"}},
                    context={"dry_run": False, "client": object(),
                             "symbol": "INFY", "quantity": 500})
    assert plan.sends_order is False
    assert plan.to_dict()["sends_order"] is False
    assert plan.quantity == 500 and plan.pov == pytest.approx(0.10)


# --------------------------------------------------------------------------- #
# 4. AGENT TEAM / STAGE GATE
# --------------------------------------------------------------------------- #
def test_shift_ledger_never_echoes_a_trade_permission():
    from quant.models.events.agent_team import run_shift

    ledger = run_shift({"stages": {"economic": {"ok": True}}, "ready_to_trade": True})
    assert ledger["ready_to_trade"] is None
    assert ledger["advice_only"] is True
    assert "economic" in ledger["summary"]["idle"] or "economic" in ledger["summary"]["worked"]


def test_missing_report_still_produces_a_ledger():
    from quant.models.events.agent_team import run_shift

    ledger = run_shift(None)
    # Blue still builds the roster so the operator sees every station; what must
    # be empty is the WORK, and the ledger must say why.
    assert ledger["stop_reason"]
    assert all(row["state"] in ("idle", "skipped") for row in ledger["stations"])
    assert ledger["summary"]["worked"] == []


@BLUE_TEAM
def test_stage_gate_blocks_on_missing_evidence_and_says_so():
    from quant.models.events.agent_team import check_stage

    blocked = check_stage("economic", {}, {"enabled": True})
    assert blocked["satisfied"] is False and blocked["missing"] == ["macro"]
    passed = check_stage("economic", {"macro": {"cpi": 4.0}}, {"enabled": True})
    assert passed["satisfied"] is True
    off = check_stage("economic", {}, {})
    assert off["enabled"] is False and "disabled" in off["reason"]


# --------------------------------------------------------------------------- #
# 5. OPTIMIZER
# --------------------------------------------------------------------------- #
def test_loss_verdict_requires_sharpe_and_checks_dsr_when_given():
    from quant.research.optimizer import loss_verdict

    assert loss_verdict(None, floor=1.0)["ok"] is False
    assert loss_verdict(0.5, floor=1.0)["ok"] is False
    ok = loss_verdict(1.5, floor=1.0)
    assert ok["ok"] is True and ok["deflated_check"].startswith("skipped")
    assert loss_verdict(1.5, floor=1.0, deflated=0.90)["ok"] is False


def test_optimisation_without_a_trial_log_is_refused():
    from quant.research.optimizer import run_optimisation

    result = run_optimisation("synthetic_demo", "synthetic", trial_log_path=None)
    assert result.ok is False and "trial_log" in result.error
    assert result.to_dict()["advice_only"] is True


@BLUE_OPT
def test_optimisation_runs_and_never_promotes(tmp_path):
    from quant.research.optimizer import run_optimisation, visual_workflow

    log = tmp_path / "trials.jsonl"
    result = run_optimisation("synthetic_demo", "synthetic", trial_log_path=log,
                              engine_config={"n_trials": 60, "periods": 600})
    assert result.ok is True
    # Early stopping may end the study before the cap; it must never exceed it.
    assert 0 < result.n_trials <= 60
    assert log.exists(), "an optimisation must write its trial log"
    assert any('optimizer_run' in line for line in log.read_text().splitlines())
    assert result.to_dict()["advice_only"] is True
    flow = visual_workflow(result)
    assert flow.get("ok") is True, flow.get("error")
    assert flow.get("step1_importance")


# --------------------------------------------------------------------------- #
# 6. BACKTEST
# --------------------------------------------------------------------------- #
def _bars(n: int = 60):
    return [{"ts": str(d), "open": 100 + i, "high": 101 + i, "low": 99 + i,
             "close": 100.5 + i, "volume": 1000}
            for i, d in enumerate(pd.bdate_range("2026-01-01", periods=n))]


def test_backtest_refuses_a_bad_request_before_touching_blue():
    from quant.research.backtest import run_symbol_backtest

    with pytest.raises(ValueError):
        run_symbol_backtest(_bars(1), lambda p, c: {}, {})           # too few bars
    with pytest.raises(ValueError):
        run_symbol_backtest(_bars(), "not callable", {})              # strategy not callable
    with pytest.raises(ValueError):
        run_symbol_backtest(_bars(), lambda p, c: {"target_position": 0}, {}, n_trials=0)


@BLUE_BT
def test_single_symbol_backtest_returns_fills_and_an_envelope():
    from quant.research.backtest import run_symbol_backtest

    result = run_symbol_backtest(_bars(), lambda p, c, bars=None: {"target_position": 0.5},
                                 {})
    assert result["total_bars"] == 60
    assert result["fills"], "a position change must produce a fill record"
    assert "equity" in result and len(result["equity"]) == 61


@BLUE_BT
def test_a_strategy_without_a_target_position_is_an_error_not_a_flat_run():
    from quant.research.backtest import run_symbol_backtest

    with pytest.raises(ValueError):
        run_symbol_backtest(_bars(), lambda p, c, bars=None: {"returns": [0.01]}, {})


# --------------------------------------------------------------------------- #
# 7. FEEDBACK / AUTO-TUNE
# --------------------------------------------------------------------------- #
_GOOD_REPORT = {"results": [{"symbol": "X", "approved": True, "oos_sharpe": 2.0,
                             "is_sharpe": 1.0, "oos_dsr_probability": 0.99}]}


def test_auto_tune_recomputes_the_policy_and_refuses_apply():
    from quant.feedback.auto_tune import review_batch

    honest = review_batch(_GOOD_REPORT)
    assert honest["approved"] is True and honest["applied_by_this_wrapper"] is False

    asked_to_apply = review_batch(dict(_GOOD_REPORT, mode="apply"))
    assert asked_to_apply["approved"] is False
    assert "refused" in asked_to_apply["reason"]


def test_auto_tune_fails_closed_on_missing_statistics():
    from quant.feedback.auto_tune import review_batch

    row = dict(_GOOD_REPORT["results"][0])
    row.pop("oos_dsr_probability")
    assert review_batch({"results": [row]})["approved"] is False
    assert review_batch({})["approved"] is False
    with pytest.raises(ValueError):
        review_batch(_GOOD_REPORT, {"oos_is_ratio": 1.5})


# --------------------------------------------------------------------------- #
# 8. STRATEGIES
# --------------------------------------------------------------------------- #
def test_a_live_signal_class_cannot_be_used_as_a_return_function():
    from quant.strategies import resolve_signal_fn

    class Fake:
        """Stands in for an OCL-style live signal class."""

    fn, err = resolve_signal_fn(Fake)
    assert fn is None and "live signal class" in err


@BLUE_ALPHA
def test_registry_lists_strategies_and_resolves_both_shapes():
    from quant.strategies import list_strategies, resolve_signal_fn

    rows = list_strategies()
    assert rows, "the alpha registry must report at least one strategy"
    names = {r.name for r in rows}
    assert any(r.kind == "live" for r in rows) or names

    fn, err = resolve_signal_fn("synthetic")
    assert fn is not None and err is None
    assert resolve_signal_fn("no_such_strategy")[0] is None


# --------------------------------------------------------------------------- #
# 9. LONG-ONLY (investment algo: BUY and ADD, never short)
# --------------------------------------------------------------------------- #
def test_universe_drops_short_side_products():
    from quant.data.screener_engine import select_universe

    payload = {
        "selected": ["NIFTYBEES", "BANKBEAR", "SBIBEAR", "INVESCO", "HDFCBANK"],
        "fundamentals": {"INVESCO": {"name": "Invesco Ultra Short Duration Fund"}},
    }
    kept = select_universe(payload)
    assert "BANKBEAR" not in kept and "SBIBEAR" not in kept and "INVESCO" not in kept
    assert kept == ["NIFTYBEES", "HDFCBANK"]
    # The switch exists for research on the exclusion itself; the default is long-only.
    assert "BANKBEAR" in select_universe(payload, long_only=False)


def test_a_sell_plan_is_refused_unless_it_closes_a_long():
    from quant.execution.pov_engine import PovPlan, plan_pov

    short_entry = plan_pov({"pov": {"participation_pct": 10, "quantity": 100,
                                    "symbol": "INFY", "action": "SELL"}},
                           context={"symbol": "INFY"})
    assert short_entry.status == "skipped" and short_entry.quantity == 0
    assert short_entry.reason == "long_only_no_short" and short_entry.sends_order is False

    invalid = PovPlan(status="skipped")
    assert invalid.sends_order is False


@BLUE_POV
def test_long_buy_plan_is_still_planned():
    from quant.execution.pov_engine import plan_pov

    plan = plan_pov({"pov": {"participation_pct": 5, "quantity": 200,
                             "symbol": "TCS", "action": "ADD"}},
                    context={"symbol": "TCS", "quantity": 200})
    assert plan.status == "planned" and plan.action == "ADD" and plan.quantity == 200


def test_backtest_refuses_to_score_a_short_book():
    from quant.research.backtest import run_symbol_backtest

    with pytest.raises(ValueError, match="long-only"):
        run_symbol_backtest(_bars(), lambda p, c, bars=None: {"target_position": -0.5},
                            {}, allow_short=True)


def test_signal_intent_guard_allows_buys_and_exits_only():
    from quant.strategies import guard_intent

    assert guard_intent(None) == (True, "no action")
    assert guard_intent({})[0] is True
    assert guard_intent({"action": "buy"})[0] is True
    allowed, why = guard_intent({"action": "SELL"})
    assert allowed is False and "exit_leg" in why
    assert guard_intent({"action": "SELL"}, exit_leg=True)[0] is True
    assert guard_intent({"action": "reverse"})[0] is False
