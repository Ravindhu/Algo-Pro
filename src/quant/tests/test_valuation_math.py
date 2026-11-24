"""Hand-checked tests for the valuation maths (EPV, CANAV, DCF) and the wrapper.

WHY THIS FILE EXISTS
--------------------
Before this file, no test checked the valuation numbers. The other tests only
checked that the wrapper was wired up. These tests use one small made-up company
whose answers can be worked out by hand, so a change that breaks the maths fails
here.

THE MADE-UP COMPANY
-------------------
Sales 1000, 1100, 1200, 1300, 1400 (crore). Operating margin 20 %. Tax 25 %.
Depreciation 50 every year. 20 shares at Rs 500 (market cap 10000).

HAND CALCULATION (EPV, base scenario)
-------------------------------------
Normalised revenue = median(1000..1400) = 1200
Normalised EBIT    = 20 % x 1200         = 240
NOPAT              = 240 x (1 - 0.25)    = 180
Maintenance capex  = depreciation x 1.00 = 50
Sustainable earn.  = 180 + 50 - 50       = 180
Enterprise value   = 180 / 0.10          = 1800
Per share          = 1800 / 20           = 90
Bear: capex x 1.30 = 65, earnings 165, rate 0.12 -> EV 1375, per share 68.75.
Bull: capex x 0.80 = 40, earnings 190, rate 0.09 -> EV 2111.11, per share 105.56.
"""
import pytest

from quant.data.valuation import run_valuation
from quant.engines.screener.valuations import value_company

HDR = ["", "Mar 2022", "Mar 2023", "Mar 2024", "Mar 2025", "Mar 2026"]


def _row(label, values):
    row = {HDR[0]: label}
    row.update({h: str(v) for h, v in zip(HDR[1:], values)})
    return row


def _company(*, with_pl=True):
    pl_rows = [
        _row("Sales", [1000, 1100, 1200, 1300, 1400]),
        _row("Operating Profit", [200, 220, 240, 260, 280]),
        _row("OPM %", [20, 20, 20, 20, 20]),
        _row("Depreciation", [50, 50, 50, 50, 50]),
        _row("Tax %", [25, 25, 25, 25, 25]),
    ]
    return {
        "profit_loss": {"headers": HDR, "rows": pl_rows if with_pl else []},
        "balance_sheet": {"headers": HDR, "rows": []},
        "cash_flow": {"headers": HDR, "rows": []},
        "ratios": {},
        "top_ratios": [
            {"name": "Current Price", "value": "500"},
            {"name": "Market Cap", "value": "10000"},
        ],
    }


@pytest.fixture(scope="module")
def result():
    return value_company(_company(), symbol="TEST", basis="consolidated")


def test_epv_base_matches_hand_calculation(result):
    base = result.as_dict()["epv"]["base"]
    assert base["status"] == "ok"
    assert base["normalized_revenue"] == pytest.approx(1200.0)
    assert base["normalized_ebit"] == pytest.approx(240.0)
    assert base["nopat"] == pytest.approx(180.0)
    assert base["maintenance_capex"] == pytest.approx(50.0)
    assert base["sustainable_earnings"] == pytest.approx(180.0)
    assert base["enterprise_value"] == pytest.approx(1800.0)
    assert base["per_share"] == pytest.approx(90.0)


def test_epv_bear_matches_hand_calculation(result):
    bear = result.as_dict()["epv"]["bear"]
    assert bear["maintenance_capex"] == pytest.approx(65.0)
    assert bear["sustainable_earnings"] == pytest.approx(165.0)
    assert bear["enterprise_value"] == pytest.approx(1375.0)
    assert bear["per_share"] == pytest.approx(68.75)


def test_epv_bull_matches_hand_calculation(result):
    bull = result.as_dict()["epv"]["bull"]
    assert bull["maintenance_capex"] == pytest.approx(40.0)
    assert bull["sustainable_earnings"] == pytest.approx(190.0)
    assert bull["enterprise_value"] == pytest.approx(190.0 / 0.09)
    assert bull["per_share"] == pytest.approx(190.0 / 0.09 / 20)


def test_epv_scenarios_are_ordered_bear_below_base_below_bull(result):
    epv = result.as_dict()["epv"]
    assert epv["bear"]["per_share"] < epv["base"]["per_share"] < epv["bull"]["per_share"]


def test_epv_flags_debt_and_capex_assumptions(result):
    # Missing debt and a proxied capex must be listed, never silently dropped.
    codes = {a["code"] for a in result.as_dict()["epv"]["base"]["assumptions"]}
    assert {"A1", "A9"} <= codes


def test_missing_profit_and_loss_gives_no_epv():
    res = value_company(_company(with_pl=False), symbol="TEST", basis="consolidated")
    epv = res.as_dict()["epv"]["base"]
    assert epv["status"] == "insufficient_data"
    assert epv["enterprise_value"] is None
    assert epv["per_share"] is None


def test_canav_and_dcf_refuse_without_their_inputs(result):
    # These synthetic snapshots have no equity or cash-flow rows, so the models
    # must say "insufficient data" instead of inventing a value.
    data = result.as_dict()
    assert data["canav"]["base"]["status"] == "insufficient_data"
    assert data["canav"]["base"]["per_share"] is None
    assert data["dcf"]["status"] == "insufficient_data"
    assert data["dcf"]["per_share"] is None


def test_wrapper_returns_advice_only_and_the_valuation():
    out = run_valuation(inputs={"companies": {"TEST": _company()}})
    assert out["advice_only"] is True
    assert out["ok"] is True
    assert "TEST" in out["valuations"]


def test_wrapper_with_no_inputs_values_nothing():
    out = run_valuation(inputs={})
    assert out["advice_only"] is True
    assert out["valuations"] == {}
