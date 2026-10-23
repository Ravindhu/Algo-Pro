"""Regression tests for the 2026-10-22 Economic -> POV audit findings.

Each test pins ONE defect that was found by tracing the live money-path. They
are deliberately narrow: a failure here means that exact bug came back.

Findings covered
----------------
F-01  Economic ``gross_exposure_multiplier`` never reached the Portfolio book.
F-02  The POV slice budget ignored the economic tilt (weights-only de-risking).
F-03  Portfolio sleeves (indices) never name the execution symbol (stock), so
      the slice refused to trade at all instead of falling back to the budget.
F-04  ``_safe_float`` was defined twice at module scope; the second definition
      returned ``int(float(value))`` and truncated every fractional price.
F-05  Zero (stale) sleeve volatility was silently replaced by the default
      without an operator-visible warning.
F-06  An up-tilt (>1.0) pushed gross past the absolute cash-buffer cap.
F-07  Portfolio's dead ``participation_pct`` scaling (key never present).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "Blue_engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "Blue_engine"))

from blue_engine.portfolio.portfolio_engine import PortfolioEngine  # noqa: E402
from blue_engine.pov.pov_engine import PovEngine, _economic_execution_guard  # noqa: E402
from blue_engine.pov.slicers import pov as pov_mod  # noqa: E402

CASH_BUFFER_CAP = 0.85  # default max_gross 1.0 / min_cash_buffer_pct 0.15


def _book(mult: float) -> dict:
    return {
        "portfolio_inputs": {
            "universe": ["NIFTY", "BANKNIFTY", "NIFTYIT"],
            "vols": {"NIFTY": 0.14, "BANKNIFTY": 0.19, "NIFTYIT": 0.17},
            "betas": {"NIFTY": 1.0, "BANKNIFTY": 1.15, "NIFTYIT": 0.90},
            "sector_by_symbol": {"NIFTY": "Broad", "BANKNIFTY": "Banking",
                                 "NIFTYIT": "IT"},
            "capital": 1_000_000.0,
        },
        "economic": {"data": {"regime": "NEUTRAL", "risk_on_score": 0.0,
                              "gross_exposure_multiplier": mult}},
    }


def _engine() -> PortfolioEngine:
    return PortfolioEngine({"optimiser": "risk_parity", "sizing_enabled": False,
                            "rebalance_band_pct": 0.0})


class _RecordingSlicer:
    """Slicer double that records the config and intent it was handed."""

    def __init__(self, client, cfg):
        self.config = cfg
        self.recorded = cfg
        self.intent = None

    def execute(self, intent):
        self.intent = intent
        return {"filled": 0, "target": intent["quantity"], "children": [],
                "child_orders": 0}


# --------------------------------------------------------------------------
# F-01 / F-06 : the multiplier reaches the book, and never breaks a hard cap
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mult, expected", [
    (0.74, round(CASH_BUFFER_CAP * 0.74, 3)),   # risk-off shrinks the book
    (1.30, CASH_BUFFER_CAP),                    # risk-on cannot breach the cap
])
def test_economic_multiplier_scales_the_book(mult, expected):
    result = _engine().run(_book(mult))
    data = result.data
    tilt = data["economic_tilt"]
    assert tilt["applied"] is True, "economic multiplier was ignored"
    assert tilt["multiplier"] == pytest.approx(mult)
    assert data["gross"] == pytest.approx(expected, abs=0.01)
    assert data["gross"] <= CASH_BUFFER_CAP + 1e-9, "hard cap was breached"


def test_neutral_multiplier_is_not_reported_as_applied():
    data = _engine().run(_book(1.0)).data
    assert data["economic_tilt"]["applied"] is False
    assert data["gross"] == pytest.approx(CASH_BUFFER_CAP, abs=0.01)


# --------------------------------------------------------------------------
# F-02 : execution quantity honours the tilt
# --------------------------------------------------------------------------
def test_economic_guard_reduces_execution_quantity():
    portfolio = {"economic_tilt": {"applied": True, "multiplier": 0.5,
                                   "regime": "RISK_OFF"}}
    guard = _economic_execution_guard(portfolio, 1000)
    assert guard["applied"] is True
    assert guard["allowed_quantity"] == 500
    assert guard["requested_quantity"] == 1000


def test_economic_guard_never_increases_quantity():
    portfolio = {"economic_tilt": {"applied": True, "multiplier": 1.5,
                                   "regime": "RISK_ON"}}
    guard = _economic_execution_guard(portfolio, 1000)
    assert guard["allowed_quantity"] == 1000


def test_economic_guard_inactive_without_tilt():
    guard = _economic_execution_guard({"economic_tilt": {"applied": False}}, 1000)
    assert guard["applied"] is False
    assert "allowed_quantity" not in guard


# --------------------------------------------------------------------------
# F-03 / F-02 end-to-end: indices-vs-stock sleeve mismatch must still trade
# --------------------------------------------------------------------------
def test_slice_falls_back_to_budget_then_applies_tilt():
    engine = PovEngine(
        {"pov": {"quantity": 1000, "participation_pct": 5.0,
                 "duration_seconds": 60, "poll_interval_seconds": 1}},
        slicer_factory=_RecordingSlicer)
    portfolio = {"data": {
        "target_weights": {"NIFTY": 0.4, "BANKNIFTY": 0.2},
        "gross": 0.6, "cash_weight": 0.4,
        "capital": 1_000_000.0, "price": 1000.0,
        "economic_tilt": {"applied": True, "multiplier": 0.5,
                          "regime": "RISK_OFF", "risk_on_score": -0.5},
    }}
    result = engine.run({"dry_run": False, "client": object(),
                         "portfolio": portfolio,
                         "symbol": "INFY", "exchange": "NSE",
                         "action": "BUY", "quantity": 1000})
    sliced = result.data["slice"]
    assert sliced["status"] == "executed", sliced
    # 1000 requested -> 600 (book budget 0.6 x 1,000,000 / 1000) -> 300 (x0.5)
    assert sliced["summary"]["target"] == 300
    guards = [w for w in result.warnings if "budget" in w]
    assert guards, "sleeve mismatch must be reported, not silently ignored"
    tilts = [w for w in result.warnings if "economic multiplier" in w]
    assert tilts, "tilt trimming must be reported"


def test_slice_refuses_a_symbol_with_no_price_to_size_the_budget():
    """A zero sleeve weight must fail closed even if a book exists."""
    engine = PovEngine({"pov": {"quantity": 100}},
                       slicer_factory=_RecordingSlicer)
    portfolio = {"data": {"target_weights": {"INFY": 0.0, "TCS": 0.2},
                          "capital": 1_000_000.0, "price": 1000.0}}
    result = engine.run({"client": object(), "portfolio": portfolio,
                         "symbol": "INFY", "exchange": "NSE",
                         "action": "BUY", "quantity": 100})
    assert result.data["slice"]["status"] == "skipped", result.data["slice"]
    assert result.data["slice"]["reason"] in (
        "symbol not in target portfolio",)


def test_slice_caps_at_sleeve_allocation_when_names_match():
    engine = PovEngine({"pov": {"quantity": 5000}},
                       slicer_factory=_RecordingSlicer)
    portfolio = {"data": {"target_weights": {"INFY": 0.30},
                          "capital": 1_000_000.0, "price": 1000.0}}
    result = engine.run({"dry_run": False, "client": object(),
                         "portfolio": portfolio,
                         "symbol": "INFY", "exchange": "NSE",
                         "action": "BUY", "quantity": 5000})
    assert result.data["slice"]["summary"]["target"] == 300  # 0.30 x 1e6 / 1000


# --------------------------------------------------------------------------
# F-17 : dry_run must gate submission even when a live client is supplied
# --------------------------------------------------------------------------
def test_dry_run_is_the_default_and_never_submits():
    """A client alone must NOT be enough to place an order (audit F-17)."""
    calls = []

    class _Client:
        def placeorder(self, **kwargs):
            calls.append(kwargs)
            return {"status": "success", "order_id": "X"}

    engine = PovEngine({"pov": {"quantity": 100}},
                       slicer_factory=_RecordingSlicer)
    result = engine.run({"client": _Client(), "symbol": "INFY",
                         "exchange": "NSE", "action": "BUY",
                         "quantity": 100})
    assert result.data["slice"]["status"] == "dry_run"
    assert calls == [], "dry_run must never reach placeorder"


def test_explicit_live_mode_can_execute():
    """The same call with dry_run=False does submit — proving the gate is not a wall."""
    engine = PovEngine({"pov": {"quantity": 100}},
                       slicer_factory=_RecordingSlicer)
    result = engine.run({"dry_run": False, "client": object(),
                         "symbol": "INFY", "exchange": "NSE",
                         "action": "BUY", "quantity": 100})
    assert result.data["slice"]["status"] == "executed"


# --------------------------------------------------------------------------
# F-04 : one _safe_float, and it preserves fractions
# --------------------------------------------------------------------------
def test_safe_float_preserves_fractional_prices():
    assert pov_mod._safe_float("1487.35") == pytest.approx(1487.35)
    assert isinstance(pov_mod._safe_float("1487.35"), float)
    assert pov_mod._safe_float(None) == 0.0


def test_safe_float_is_defined_once():
    src = (ROOT / "Blue_engine" / "pov" / "slicers" / "pov.py").read_text(
        encoding="utf-8")
    assert src.count("def _safe_float(") == 1, (
        "a second module-level _safe_float shadows the first; the last "
        "definition wins at call time")


# --------------------------------------------------------------------------
# F-05 : stale sleeve inputs are visible
# --------------------------------------------------------------------------
def test_zero_volatility_is_reported():
    result = _engine().run({"portfolio_inputs": {
        "universe": ["NIFTY", "BANKNIFTY"],
        "vols": {"NIFTY": 0.0, "BANKNIFTY": 0.19},
        "betas": {"NIFTY": 1.0, "BANKNIFTY": 1.15}}})
    flagged = [w for w in result.warnings if "invalid volatility" in w]
    assert flagged, "a zero-volatility (stale) sleeve must be reported"


def test_zero_volatility_warning_is_not_duplicated():
    result = _engine().run({"portfolio_inputs": {
        "universe": ["NIFTY", "BANKNIFTY"],
        "vols": {"NIFTY": 0.0, "BANKNIFTY": 0.19},
        "betas": {"NIFTY": 1.0, "BANKNIFTY": 1.15}}})
    flagged = [w for w in result.warnings if "invalid volatility" in w]
    assert len(flagged) == len(set(flagged)), flagged


def test_implausible_beta_is_reported():
    result = _engine().run({"portfolio_inputs": {
        "universe": ["NIFTY", "BANKNIFTY"],
        "vols": {"NIFTY": 0.14, "BANKNIFTY": 0.19},
        "betas": {"NIFTY": 12.0, "BANKNIFTY": 1.15}}})
    assert [w for w in result.warnings if "invalid beta" in w]


# --------------------------------------------------------------------------
# F-07 : the dead participation-pct scaling is gone
# --------------------------------------------------------------------------
def test_pov_config_is_not_scaled_by_a_missing_portfolio_key():
    from blue_engine.pov.pov_engine import _build_pov_config
    cfg = _build_pov_config({"participation_pct": 10.0}, "INFY", "NSE", "BUY",
                            100, {"gross_exposure_multiplier": 0.1})
    assert cfg["participation_pct"] == 10.0
