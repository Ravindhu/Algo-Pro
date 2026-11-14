"""Stage 6 (Portfolio risk): sector/beta caps, tail risk, and pre-gate wiring.

The gap this closes: sector, cluster and beta caps were documented as a stage and
NOTHING on the trade path called them. A book could hold three banks at 15% each
and every individual order would pass every per-order check, because every
per-order check looks at one order.

`tail_risk.py` had the same problem - 186 lines of VaR/CVaR and five named India
scenarios, wired to nothing.
"""
from datetime import date

import pytest

from quant.core.types import TradeIntent
from quant.portfolio.risk_limits import (PortfolioRiskConfig, book_summary,
                                         portfolio_risk_checks,
                                         proposed_weights)
from quant.risk.pre_gate import PreGate, PreGateConfig

BANKS = {"HDFCBANK": 0.15, "ICICIBANK": 0.15, "SBIN": 0.15, "TCS": 0.10}
SECTORS = {"HDFCBANK": "BANK", "ICICIBANK": "BANK", "SBIN": "BANK", "TCS": "IT"}


def _cfg(**over):
    base = dict(max_sector_pct=0.40, max_beta=1.2, sector_map=SECTORS)
    base.update(over)
    return PortfolioRiskConfig(**base)


# --------------------------------------------------------------------------- #
# sector caps
# --------------------------------------------------------------------------- #
def test_concentrated_sector_is_caught():
    """Three banks at 15% is 45% of the book - the case no per-order check sees."""
    reasons = portfolio_risk_checks(BANKS, config=_cfg())
    assert any("BANK exposure" in r for r in reasons)


def test_diversified_book_passes():
    assert portfolio_risk_checks({"HDFCBANK": 0.10, "TCS": 0.10}, config=_cfg()) == []


def test_unmapped_symbols_are_capped_as_unknown():
    """A sector bet you cannot name is still a sector bet."""
    reasons = portfolio_risk_checks({"ZZZ": 0.50}, config=_cfg())
    assert any("UNKNOWN" in r for r in reasons)


def test_unset_sector_cap_refuses():
    """'No cap configured' must never mean 'no sector risk'."""
    reasons = portfolio_risk_checks(BANKS, config=_cfg(max_sector_pct=None))
    assert any("max_sector_pct" in r for r in reasons)


def test_empty_book_refuses():
    assert portfolio_risk_checks({}, config=_cfg())


# --------------------------------------------------------------------------- #
# beta cap
# --------------------------------------------------------------------------- #
def test_beta_above_cap_is_refused():
    cfg = _cfg(max_beta=0.5, betas={"A": 2.0})
    reasons = portfolio_risk_checks({"A": 1.0}, config=cfg)
    assert any("beta" in r for r in reasons)


def test_beta_within_cap_passes():
    cfg = _cfg(max_beta=2.0, betas={"A": 1.0})
    assert not any("beta" in r for r in portfolio_risk_checks({"A": 1.0}, config=cfg))


def test_unset_beta_cap_refuses():
    reasons = portfolio_risk_checks({"A": 0.1}, config=_cfg(max_beta=None))
    assert any("max_beta" in r for r in reasons)


# --------------------------------------------------------------------------- #
# tail risk (the previously idle wrapper)
# --------------------------------------------------------------------------- #
def test_tail_limit_without_history_fails_closed():
    """An unmeasurable tail limit must refuse, never pass quietly."""
    reasons = portfolio_risk_checks({"A": 0.1}, config=_cfg(max_cvar=0.05))
    assert any("no P&L history" in r for r in reasons)


def test_tail_limit_breach_is_reported():
    cfg = _cfg(max_cvar=0.001, max_drawdown=0.90)
    history = [-0.04, -0.03, 0.01, 0.02, -0.05] * 10
    reasons = portfolio_risk_checks({"A": 0.1}, config=cfg, pnl_history=history)
    assert any("cvar" in r.lower() for r in reasons)


def test_tail_limit_within_bounds_passes():
    cfg = _cfg(max_cvar=0.50, max_drawdown=0.90)
    history = [-0.01, 0.01] * 20
    reasons = portfolio_risk_checks({"A": 0.1}, config=cfg, pnl_history=history)
    assert not any("tail risk" in r for r in reasons)


def test_book_summary_includes_named_scenarios():
    """The five India stress scenarios come through the wrapper."""
    summary = book_summary(BANKS, config=_cfg())
    assert summary["tail"]["scenarios_available"] >= 5
    names = {s["scenario"] for s in summary["tail"]["stress"]}
    assert "GFC_2008" in names


def test_book_summary_reports_unmapped_symbols():
    summary = book_summary({"ZZZ": 0.1}, config=_cfg(sector_map={}))
    assert summary["unmapped"] == ["ZZZ"]


# --------------------------------------------------------------------------- #
# proposed book
# --------------------------------------------------------------------------- #
def test_proposed_weights_adds_the_order():
    out = proposed_weights({"A": 0.10}, "B", 0.05, capital=1_000_000)
    assert out["B"] == pytest.approx(0.05)
    assert out["A"] == pytest.approx(0.10)


def test_proposed_weights_does_not_mutate():
    book = {"A": 0.10}
    proposed_weights(book, "B", 0.05, capital=1_000_000)
    assert book == {"A": 0.10}


def test_proposed_weights_prefers_notional_when_given():
    out = proposed_weights({}, "B", None, capital=1_000_000, notional=50_000)
    assert out["B"] == pytest.approx(0.05)


def test_proposed_weights_accumulates_an_existing_holding():
    out = proposed_weights({"A": 0.10}, "A", 0.05, capital=1_000_000)
    assert out["A"] == pytest.approx(0.15)


# --------------------------------------------------------------------------- #
# config validation
# --------------------------------------------------------------------------- #
def test_negative_cap_is_refused():
    with pytest.raises(ValueError, match="max_sector_pct"):
        PortfolioRiskConfig(max_sector_pct=-0.1)


def test_zero_cap_is_refused():
    with pytest.raises(ValueError, match="max_beta"):
        PortfolioRiskConfig(max_beta=0)


# --------------------------------------------------------------------------- #
# pre-gate wiring
# --------------------------------------------------------------------------- #
def _intent():
    return TradeIntent(intent_id="I1", strategy_id="s", symbol="SBIN", side="BUY",
                       qty=10, ref_price=100.0, kind="entry", lot_size=1,
                       trade_day=date(2026, 11, 13))


def test_pre_gate_blocks_a_concentrated_book():
    gate = PreGate(PreGateConfig(max_sector_pct=0.40, max_beta=1.2), holidays=set())
    # The pre-gate resolves sectors from the screen, so pass the book explicitly
    # and rely on UNKNOWN grouping, which is capped like any other group.
    res = gate.check(_intent(), weights={"A": 0.50}, capital=1_000_000,
                     check_book_limits=True)
    assert res.ok is False
    assert any("portfolio:" in r for r in res.reasons)


def test_pre_gate_ignores_the_book_when_none_is_supplied():
    """Opt-in: an existing caller keeps its exact behaviour."""
    gate = PreGate(PreGateConfig(), holidays=set())
    assert gate.check(_intent()).ok is True


def test_book_caps_are_separately_opt_in():
    """Passing weights for the stress check must NOT silently enable the cap
    check, because that turned an unset cap into an unexpected refusal.

    The only reason this entry may still pass is the sector/beta caps being off;
    the STRESS check still runs on the supplied weights, so the refusal reason (if
    any) must not be a portfolio-cap reason.
    """
    gate = PreGate(PreGateConfig(), holidays=set())
    res = gate.check(_intent(), weights={"A": 0.9}, capital=1_000_000)
    assert not any(r.startswith("portfolio:") for r in res.reasons)


def test_exit_is_never_blocked_by_book_caps():
    gate = PreGate(PreGateConfig(max_sector_pct=0.01, max_beta=0.01), holidays=set())
    exit_intent = TradeIntent(intent_id="E1", strategy_id="s", symbol="SBIN",
                              side="SELL", qty=10, ref_price=100.0, kind="exit",
                              lot_size=1, trade_day=date(2026, 11, 13))
    assert gate.check(exit_intent, weights={"A": 0.9},
                      check_book_limits=True).ok is True
