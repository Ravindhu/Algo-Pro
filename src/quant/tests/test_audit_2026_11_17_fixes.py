"""Regression tests for docs/algo-pro-audit-2026-11-17.md.

Each test names the audit item it pins. Read the audit alongside this file: the
tests assert the FIXED behaviour, not the behaviour the audit found.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src" / "quant"))
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from quant.engines.backtest import metrics as M  # noqa: E402
from quant.engines.backtest.costs import CostModel  # noqa: E402
from quant.engines.core.risk_engine import RiskEngine  # noqa: E402
from quant.engines.portfolio.optim import cost_aware  # noqa: E402
from quant.execution.pov_sizing import slippage_penalised_rate  # noqa: E402

LIMITS = ROOT / "config" / "risk_limits.yaml"


# --------------------------------------------------------------------------- #
# Item 1: one slippage setting drives the simulator AND the planner.
# --------------------------------------------------------------------------- #
def test_one_slippage_key_moves_the_simulator():
    model = CostModel(cost_config={"slippage": 17.0}, latency_bps=0.0,
                      impact_coefficient=0.0)
    assert model.slippage_bps == pytest.approx(17.0)


def test_slippage_is_not_double_counted_as_a_statutory_rate():
    """The planner charges slippage per side and the simulator charges it as a
    price move. If the simulator ALSO carried it as a 'statutory' rate, a
    configured 17 bps would be charged twice."""
    model = CostModel(cost_config={"slippage": 17.0}, latency_bps=0.0,
                      impact_coefficient=0.0)
    assert "slippage" not in model.rates
    assert model.rates["stt"] == cost_aware.DEFAULT_COST_BPS["stt"]


def test_planner_and_simulator_round_trip_agree_apart_from_latency_and_impact():
    """Same config, same statutory total. The audit's finding 1 was that changing
    ``slippage`` moved the planner and not the simulator."""
    model = CostModel(latency_bps=0.0, impact_coefficient=0.0)
    planner = cost_aware._round_trip_bps(cost_aware.planner_costs())
    # The simulator's statutory round trip EXCLUDES the market terms, so it must
    # match the planner's total minus slippage (charged once per side).
    assert model.round_trip_bps() == pytest.approx(
        planner - 2 * cost_aware.DEFAULT_SLIPPAGE_BPS, abs=1e-9)

    # And a configured slippage term reaches BOTH: the planner as bps, the
    # simulator as a price move, each exactly once per side.
    configured = cost_aware.planner_costs({"slippage": 20.0})
    assert (cost_aware._round_trip_bps(configured) - planner
            == pytest.approx(2 * (20.0 - cost_aware.DEFAULT_SLIPPAGE_BPS)))
    assert CostModel(cost_config={"slippage": 20.0}).slippage_bps == pytest.approx(20.0)


def test_default_stack_has_no_slippage_key():
    """The statutory dict must stay statutory: the simulator charges slippage in
    the fill price, so a key here would be a second charge for one config."""
    assert "slippage" not in cost_aware.DEFAULT_COST_BPS
    assert cost_aware.DEFAULT_SLIPPAGE_BPS == 5.0
    # The planner still charges it, once per side.
    assert cost_aware.planner_costs()["slippage"] == 5.0
    assert cost_aware.planner_costs({"slippage": 12.0})["slippage"] == 12.0


# --------------------------------------------------------------------------- #
# Item 2: a flat per-order fee is charged in rupees, not bps.
# --------------------------------------------------------------------------- #
def test_flat_fee_replaces_the_bps_brokerage_rate():
    model = CostModel(cost_config={"flat_brokerage_per_order": 20.0,
                                   "brokerage": 3.0})
    assert model.flat_fee() == pytest.approx(20.0)
    # Brokerage bps is zeroed when a flat fee is configured.
    assert model.charges_bps(is_buy=True) == pytest.approx(
        CostModel(cost_config={"brokerage": 0.0}).charges_bps(is_buy=True))


def test_a_small_ticket_pays_a_larger_effective_bps_with_a_flat_fee():
    model = CostModel(cost_config={"flat_brokerage_per_order": 20.0,
                                   "brokerage": 3.0})
    small = model.charges_amount(is_buy=True, notional=2_000.0)
    large = model.charges_amount(is_buy=True, notional=200_000.0)
    small_bps = small / 2_000.0 * 10000.0
    large_bps = large / 200_000.0 * 10000.0
    assert small_bps > large_bps


def test_intraday_stt_is_overridable_and_stamp_duty_is_buy_side_only():
    model = CostModel(cost_config={"stt": 2.5})
    assert model.charges_bps(is_buy=True) - model.charges_bps(is_buy=False) == pytest.approx(
        model.rates["stamp_duty"])


# --------------------------------------------------------------------------- #
# Item 3: profit factor is measured on the same population as win rate.
# --------------------------------------------------------------------------- #
def test_profit_factor_uses_net_pnl():
    trades = [{"gross_pnl": 100.0, "pnl": -10.0},   # won before costs, lost after
              {"gross_pnl": -50.0, "pnl": -60.0}]
    out = M.compute_metrics(equity=[1000.0, 930.0], trades=trades,
                            total_bars=1, bars_in_market=1)
    # Net: no winner at all, so the factor is 0 (wins 0 / losses 70).
    assert out["profit_factor"] == pytest.approx(0.0)
    assert out["n_wins"] == 0
    # Gross still reported, separately, for decomposition.
    assert out["gross_profit_factor"] == pytest.approx(2.0)
    # The old bug: profit factor 2.0 next to a 0% win rate.
    assert out["profit_factor"] != out["gross_profit_factor"]


def test_profit_factor_matches_win_and_loss_totals():
    trades = [{"pnl": 30.0}, {"pnl": 20.0}, {"pnl": -25.0}]
    out = M.compute_metrics(equity=[1000.0, 1025.0], trades=trades,
                            total_bars=1, bars_in_market=1)
    assert out["profit_factor"] == pytest.approx(2.0)
    assert out["net_profit_total"] == pytest.approx(50.0)
    assert out["net_loss_total"] == pytest.approx(-25.0)


def test_sortino_docstring_matches_the_code():
    """Docstring is the contract; the code already used the standard formula."""
    doc = M.sortino_ratio.__doc__ or ""
    assert "FULL sample" in doc
    assert "mean of the downside alone" in doc


# --------------------------------------------------------------------------- #
# Item 4: null cap == not set == fail closed, one rule everywhere.
# --------------------------------------------------------------------------- #
def test_repo_limits_header_does_not_promise_that_null_disables_a_check():
    text = LIMITS.read_text(encoding="utf-8")
    assert "switches its check off" not in text
    assert "NOT SET" in text


def test_declared_fraction_slippage_under_the_cap_passes():
    out = RiskEngine({"max_allowed_slippage_pct": 0.005}).run({
        "dry_run": True,
        "risk_inputs": {"observed_slippage_pct": 0.002, "slippage_unit": "fraction"},
    })
    assert out.data["risk_veto_active"] is False


def test_declared_percent_slippage_is_converted_before_comparing():
    out = RiskEngine({"max_allowed_slippage_pct": 0.005}).run({
        "dry_run": True,
        # 0.2% = 20 bps, under a 50 bps cap. The old code compared 0.2 > 0.005.
        "risk_inputs": {"observed_slippage_pct": 0.2, "slippage_unit": "percent"},
    })
    assert out.data["risk_veto_active"] is False


def test_percent_slippage_over_the_cap_vetoes():
    out = RiskEngine({"max_allowed_slippage_pct": 0.005}).run({
        "dry_run": True,
        "risk_inputs": {"observed_slippage_pct": 5.0, "slippage_unit": "percent"},
    })
    assert out.data["risk_veto_active"] is True


def test_undeclared_slippage_unit_vetoes():
    out = RiskEngine({"max_allowed_slippage_pct": 0.005}).run({
        "dry_run": True,
        "risk_inputs": {"observed_slippage_pct": 0.002},
    })
    assert out.data["risk_veto_active"] is True
    assert any("unit not declared" in b for b in out.data["breaches"])


# --------------------------------------------------------------------------- #
# Item 7: the rationale names the reason.
# --------------------------------------------------------------------------- #
def test_veto_rationale_is_not_truncated():
    out = RiskEngine().run({
        "dry_run": True,
        "risk_inputs": {"corrupted_data": True, "conflicting_reports": True,
                        "network_down": True, "latency_anomaly": True,
                        "risk_veto_active": True, "veto_reason": "manual stop",
                        "latency_ms": 99999},
    })
    assert len((out.data["veto_reason"] or "").split()) > 15
    assert out.data["veto_reason"] == "; ".join(out.data["breaches"])


# --------------------------------------------------------------------------- #
# Item 9: the reconciler measures both sides.
# --------------------------------------------------------------------------- #
def test_reconciler_has_no_buy_only_shortcut():
    import inspect

    from quant.engines.backtest.loops import SlippageReconciler
    src = inspect.getsource(SlippageReconciler.reconcile)
    assert 'side != "BUY"' not in src


# --------------------------------------------------------------------------- #
# Item 10: the slippage penalty can only reduce the rate.
# --------------------------------------------------------------------------- #
def test_penalty_above_one_cannot_raise_the_rate():
    assert slippage_penalised_rate(15.0, over_threshold=True, penalty=3.0,
                                   floor_pct=0.0) == pytest.approx(15.0)


def test_penalty_still_reduces_and_respects_the_floor():
    assert slippage_penalised_rate(15.0, over_threshold=True, penalty=0.5,
                                   floor_pct=5.0) == pytest.approx(7.5)
    assert slippage_penalised_rate(15.0, over_threshold=False, penalty=0.1,
                                   floor_pct=5.0) == pytest.approx(15.0)


# --------------------------------------------------------------------------- #
# Item 11: avg price lands on the claimed child.
# --------------------------------------------------------------------------- #
def test_avg_price_lands_on_the_claimed_child_not_the_list_tail():
    from quant.engines.core import ledger

    ledger._degrade_db(RuntimeError("test: in-memory ledger"))
    ledger._IN_MEMORY_EXECUTIONS.clear()
    eid = "T-AVG-PRICE"
    ledger.create_execution(execution_id=eid, strategy="T", symbol="X",
                            exchange="NSE", action="BUY", total_quantity=100,
                            mode="paper", phase="accumulating")
    # Two children claimed "concurrently": the second claim appended before the
    # first child's price was written.
    first = ledger.claim_child(eid, 10, 0.0, "accumulating")
    ledger.finish_child(eid, first, success=True, order_id="O1")
    ledger.claim_child(eid, 10, 0.0, "accumulating")
    # This call claims child 3 and must write the price to CHILD 3, not to the
    # list tail as it stood a moment earlier.
    claimed = ledger.record_child(execution_id=eid, order_id="O2", quantity=10,
                                  status="open", avg_price=101.0)

    row = ledger._IN_MEMORY_EXECUTIONS[eid]
    by_number = {c["child_number"]: c for c in row.children}
    assert claimed == 3
    assert by_number[claimed]["avg_price"] == pytest.approx(101.0)
    assert "avg_price" not in by_number[1]
    assert "avg_price" not in by_number[2]


# --------------------------------------------------------------------------- #
# Smaller items: the SELL fill and the marked open position.
# --------------------------------------------------------------------------- #
def test_sell_fill_is_below_the_reference_by_the_same_bps():
    model = CostModel(slippage_bps=50.0, latency_bps=0.0, impact_coefficient=0.0)
    buy = model.fill_price(side="BUY", reference_price=100.0)["price"]
    sell = model.fill_price(side="SELL", reference_price=100.0)["price"]
    assert buy == pytest.approx(100.5)
    assert sell == pytest.approx(99.5)


def test_an_open_position_is_marked_with_its_exit_cost():
    from quant.engines.backtest.engine import BacktestConfig, run_backtest

    rows = [
        {"ts": "2025-01-01", "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 10000},
        {"ts": "2025-01-02", "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 10000},
    ]
    config = BacktestConfig(cost_model=CostModel(
        slippage_bps=0, latency_bps=0, impact_coefficient=0))
    result = run_backtest(rows, lambda p, c, b: 1.0, {}, config=config)
    trade = result.trades[-1]
    assert trade["status"] == "OPEN"
    # entry cost + the hypothetical exit cost, and pnl net of both.
    assert trade["costs"] > 0
    assert trade["pnl"] == pytest.approx(
        trade["gross_pnl"] - trade["costs"])
