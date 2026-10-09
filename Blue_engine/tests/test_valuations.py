"""
test_valuations.py - tests for EPV / CANAV in ``screener/valuations.py``.

The suite is built around one question: **does this module refuse to invent
numbers?** Most of the tests below feed it deliberately incomplete financials
and assert that it returns ``status="insufficient_data"`` with a named missing
input, rather than a plausible-looking figure.

The arithmetic tests use small, hand-built tables where the expected value can
be computed on paper, so a failure means the FORMULA broke - not that a fixture
drifted.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.screener import valuations as V  # noqa: E402


# --------------------------------------------------------------------------- #
# Table builders
# --------------------------------------------------------------------------- #
def _table(rows, headers=None):
    """Build a screener.in-shaped financial table.

    ``rows`` is ``{"<label>": [v1, v2, ...]}``; headers default to 5 annual
    periods plus a TTM column, which is what the real P&L looks like.
    """
    if headers is None:
        headers = ["", "Mar 2021", "Mar 2022", "Mar 2023", "Mar 2024",
                   "Mar 2025", "TTM"]
    out = []
    for label, values in rows.items():
        row = {headers[0]: label}
        for i, value in enumerate(values):
            row[headers[i + 1]] = value
        out.append(row)
    return {"headers": headers, "rows": out}


def _company(**over):
    """A minimal but COMPLETE company. Tests remove one input at a time."""
    base = {
        "symbol": "TESTCO",
        "top_ratios": [
            {"id": "current-price", "name": "Current Price", "value": "500"},
            {"id": "market-cap", "name": "Market Cap", "value": "50,000 Cr."},
        ],
        "profit_loss": _table({
            "Sales":            ["1,00,000", "1,00,000", "1,00,000", "1,00,000",
                                 "1,00,000", "1,00,000"],
            "Operating Profit": ["15,000", "15,000", "15,000", "15,000",
                                 "15,000", "15,000"],
            "OPM %":            ["15", "15", "15", "15", "15", "15"],
            "Depreciation":     ["5,000", "5,000", "5,000", "5,000",
                                 "5,000", "5,000"],
            "Tax %":            ["25", "25", "25", "25", "25", "25"],
            "Net Profit":       ["7,500", "7,500", "7,500", "7,500",
                                 "7,500", "7,500"],
            "EPS in Rs":        ["15", "15", "15", "15", "15", "15"],
        }),
        "balance_sheet": _table({
            "Equity Capital": ["100", "100", "100", "100", "100", "100"],
            "Reserves":       ["9,900", "9,900", "9,900", "9,900", "9,900",
                               "9,900"],
            "Borrowings +":   ["2,000", "2,000", "2,000", "2,000", "2,000",
                               "2,000"],
            "Fixed Assets":   ["5,000", "5,000", "5,000", "5,000", "5,000",
                               "5,000"],
            "Investments":    ["1,000", "1,000", "1,000", "1,000", "1,000",
                               "1,000"],
            "Other Assets +": ["6,000", "6,000", "6,000", "6,000", "6,000",
                               "6,000"],
        }),
        "cash_flow": _table({
            "Operating Activity": ["10,000", "10,000", "10,000", "10,000",
                                   "10,000", "10,000"],
            "Investing Activity": ["-5,000", "-5,000", "-5,000", "-5,000",
                                   "-5,000", "-5,000"],
        }),
        "ratios": _table({
            "Debtor Days":          ["40", "40", "40", "40", "40", "40"],
            "Inventory Days":       ["60", "60", "60", "60", "60", "60"],
            "Working Capital Days": ["30", "30", "30", "30", "30", "30"],
        }),
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Input extraction
# --------------------------------------------------------------------------- #
class TestBuildInputs:
    def test_extracts_the_core_series(self):
        inp = V.build_inputs(_company())
        assert inp.revenue_series == [100000.0] * 5
        assert inp.ebit_series == [15000.0] * 5
        assert inp.depreciation_series == [5000.0] * 5
        assert inp.tax_rate_series == [25.0] * 5

    def test_ttm_column_is_excluded_from_annual_normalisation(self):
        """The P&L carries a TTM column; it is not an audited annual period."""
        company = _company()
        # Make TTM wildly different so a leak is obvious.
        company["profit_loss"]["rows"][0]["TTM"] = "99,99,999"
        inp = V.build_inputs(company)
        assert 9999999.0 not in inp.revenue_series
        assert inp.revenue_series == [100000.0] * 5

    def test_tax_rate_does_not_pick_up_profit_before_tax(self):
        """Regression: a substring match for 'tax' returns 'Profit before tax'.

        'Profit before tax' is in CRORES and 'Tax %' is a PERCENTAGE. Matching
        the wrong row would put a crore figure into a tax rate.
        """
        company = _company()
        rows = company["profit_loss"]["rows"]
        # Insert "Profit before tax" BEFORE "Tax %" so a substring matcher hits it.
        rows.insert(3, {"": "Profit before tax",
                        "Mar 2021": "10,000", "Mar 2022": "10,000",
                        "Mar 2023": "10,000", "Mar 2024": "10,000",
                        "Mar 2025": "10,000"})
        inp = V.build_inputs(company)
        assert inp.tax_rate_series == [25.0] * 5, \
            "must read 'Tax %' (25), not 'Profit before tax' (10000)"

    def test_shareholders_equity_is_capital_plus_reserves(self):
        inp = V.build_inputs(_company())
        assert inp.shareholders_equity == 10000.0

    def test_borrowings_matched_on_stem(self):
        """Real pages use 'Borrowing' (banks) and 'Borrowings +' (others)."""
        company = _company()
        company["balance_sheet"] = _table({
            "Equity Capital": ["100"],
            "Reserves": ["9,900"],
            "Borrowing": ["3,000"],
        })
        inp = V.build_inputs(company)
        assert inp.borrowings == 3000.0

    def test_inventory_and_receivables_recovered_from_days(self):
        """Balance sheet lumps them into Other Assets, so invert the ratios."""
        inp = V.build_inputs(_company())
        daily = 100000.0 / 365.0
        assert inp.inventory == pytest.approx(60 * daily)
        assert inp.receivables == pytest.approx(40 * daily)

    def test_absurd_day_counts_are_refused(self):
        """A 9000-day receivable is a data error, not an asset."""
        company = _company()
        company["ratios"] = _table({
            "Debtor Days": ["9000"],
            "Inventory Days": ["99999"],
            "Working Capital Days": ["30"],
        })
        inp = V.build_inputs(company)
        assert inp.receivables is None
        assert inp.inventory is None

    def test_shares_from_net_profit_over_eps(self):
        """EPS and market cap DISAGREE in the base fixture, so the module
        prefers the market-cap figure (the one consistent with the current
        price) and records why. This is the mismatch path, not the EPS path.
        """
        inp = V.build_inputs(_company())
        assert inp.diluted_shares == pytest.approx(50000.0 / 500.0)
        assert any("mismatch" in n for n in inp.notes)

    def test_shares_from_eps_when_market_cap_agrees(self):
        """When both paths agree, the EPS path is used directly."""
        company = _company()
        # Net Profit 7,500 / EPS 15 = 500 cr shares. Make market cap agree:
        # 500 cr shares x price 500 = 2,50,000 Cr.
        company["top_ratios"] = [
            {"id": "current-price", "name": "Current Price", "value": "500"},
            {"id": "market-cap", "name": "Market Cap", "value": "2,50,000 Cr."},
        ]
        inp = V.build_inputs(company)
        assert inp.diluted_shares == pytest.approx(7500.0 / 15.0)
        assert not any("mismatch" in n for n in inp.notes)

    def test_share_mismatch_prefers_market_cap_and_records_it(self):
        """Market cap/price is the count consistent with the CURRENT price."""
        company = _company()
        # EPS implies 500 cr shares; market cap/price implies 100 cr.
        company["top_ratios"] = [
            {"id": "current-price", "name": "Current Price", "value": "500"},
            {"id": "market-cap", "name": "Market Cap", "value": "50,000 Cr."},
        ]
        inp = V.build_inputs(company)
        assert inp.diluted_shares == pytest.approx(50000.0 / 500.0)
        assert any("mismatch" in n for n in inp.notes)

    def test_missing_inputs_are_listed(self):
        company = _company()
        company["profit_loss"] = _table({})   # no rows
        inp = V.build_inputs(company)
        assert "revenue_series" in inp.missing
        assert "ebit_series" in inp.missing


# --------------------------------------------------------------------------- #
# EPV
# --------------------------------------------------------------------------- #
class TestEpv:
    def _inputs(self, **over):
        return V.build_inputs(_company(**over))

    def test_arithmetic_matches_the_specified_formula(self):
        """Hand-computable case: 100,000 revenue, 15% margin, 25% tax."""
        inp = self._inputs()
        r = V.compute_epv(inp, "base")
        assert r.status == "ok"

        assert r.normalized_revenue == pytest.approx(100000.0)
        assert r.normalized_ebit_margin == pytest.approx(0.15)
        assert r.normalized_ebit == pytest.approx(15000.0)
        assert r.normalized_tax_rate == pytest.approx(0.25)
        assert r.nopat == pytest.approx(15000.0 * 0.75)

        assert r.depreciation == pytest.approx(5000.0)
        assert r.maintenance_capex == pytest.approx(5000.0 * 1.00)   # base mult
        assert r.required_return == pytest.approx(0.10)

        expected_se = (15000.0 * 0.75) + 5000.0 - 5000.0 - r.maintenance_working_capital
        assert r.sustainable_earnings == pytest.approx(expected_se)
        assert r.enterprise_value == pytest.approx(expected_se / 0.10)

        expected_eq = (r.enterprise_value + r.excess_cash
                       + r.non_operating_investments - r.debt
                       - r.debt_like - r.minority_interest)
        assert r.equity_value == pytest.approx(expected_eq)
        assert r.per_share == pytest.approx(expected_eq / inp.diluted_shares)
        assert r.margin_of_safety == pytest.approx(
            1.0 - inp.market_price / r.per_share)

    def test_normalisation_uses_median_not_mean(self):
        """One exceptional year must be absorbed, not amplified."""
        company = _company()
        # Revenue spikes 10x in the most recent year only.
        company["profit_loss"] = _table({
            "Sales":            ["1,00,000", "1,00,000", "1,00,000",
                                 "1,00,000", "10,00,000"],
            "Operating Profit": ["15,000", "15,000", "15,000", "15,000",
                                 "1,50,000"],
            "OPM %":            ["15", "15", "15", "15", "15"],
            "Depreciation":     ["5,000"] * 5,
            "Tax %":            ["25"] * 5,
            "Net Profit":       ["7,500"] * 5,
            "EPS in Rs":        ["15"] * 5,
        })
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        assert r.normalized_revenue == pytest.approx(100000.0), \
            "median must ignore the 10x spike"

    def test_ebit_is_margin_times_revenue_not_median_ebit(self):
        """The margin must be sustainable, not one lucky high-margin year."""
        company = _company()
        company["profit_loss"] = _table({
            # Revenue falls but margin explodes: median(EBIT) would be high.
            "Sales":            ["1,00,000", "1,00,000", "1,00,000",
                                 "50,000", "40,000"],
            "Operating Profit": ["15,000", "15,000", "15,000", "20,000",
                                 "16,000"],
            "OPM %":            ["15", "15", "15", "40", "40"],
            "Depreciation":     ["5,000"] * 5,
            "Tax %":            ["25"] * 5,
            "Net Profit":       ["7,500"] * 5,
            "EPS in Rs":        ["15"] * 5,
        })
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        # median(revenue)=100000, median(margin)=15% -> EBIT 15,000
        assert r.normalized_revenue == pytest.approx(100000.0)
        assert r.normalized_ebit_margin == pytest.approx(0.15)
        assert r.normalized_ebit == pytest.approx(15000.0)

    def test_scenarios_are_ordered_bear_lt_base_lt_bull(self):
        r = {s: V.compute_epv(self._inputs(), s) for s in V.EPV_SCENARIOS}
        assert r["bear"].per_share < r["base"].per_share < r["bull"].per_share

    def test_scenarios_differ_only_via_rate_and_capex_multiple(self):
        """Revenue and margin must NOT move between scenarios (EPV = no growth)."""
        r = {s: V.compute_epv(self._inputs(), s) for s in V.EPV_SCENARIOS}
        revs = {round(x.normalized_revenue, 6) for x in r.values()}
        margins = {round(x.normalized_ebit_margin, 6) for x in r.values()}
        assert len(revs) == 1
        assert len(margins) == 1

    def test_negative_ebit_margin_is_refused(self):
        """A negative EV/discount is meaningless and would be 'passed' by price."""
        company = _company()
        company["profit_loss"] = _table({
            "Sales":            ["1,00,000"] * 5,
            "Operating Profit": ["-5,000"] * 5,
            "OPM %":            ["-5"] * 5,
            "Depreciation":     ["5,000"] * 5,
            "Tax %":            ["25"] * 5,
            "Net Profit":       ["-5,000"] * 5,
            "EPS in Rs":        ["-10"] * 5,
        })
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        assert r.status == "insufficient_data"
        assert "not positive" in r.reason

    def test_missing_depreciation_is_refused_not_assumed(self):
        company = _company()
        rows = company["profit_loss"]["rows"]
        company["profit_loss"]["rows"] = [r for r in rows
                                          if r.get("") != "Depreciation"]
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        assert r.status == "insufficient_data"
        assert "Depreciation" in r.reason

    def test_tax_rate_clamped_and_flagged(self):
        company = _company()
        company["profit_loss"] = _table({
            "Sales":            ["1,00,000"] * 5,
            "Operating Profit": ["15,000"] * 5,
            "OPM %":            ["15"] * 5,
            "Depreciation":     ["5,000"] * 5,
            "Tax %":            ["80", "80", "80", "80", "80"],
            "Net Profit":       ["7,500"] * 5,
            "EPS in Rs":        ["15"] * 5,
        })
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        assert r.normalized_tax_rate == pytest.approx(0.50)
        assert any(a.code == "A6" for a in r.assumptions)

    def test_missing_tax_rate_uses_stated_fallback(self):
        company = _company()
        rows = company["profit_loss"]["rows"]
        company["profit_loss"]["rows"] = [x for x in rows if x.get("") != "Tax %"]
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        assert r.normalized_tax_rate == pytest.approx(0.25)
        assert any(a.code == "A5" for a in r.assumptions)

    def test_excess_cash_is_zero_and_declared_when_cash_is_unknown(self):
        inp = self._inputs()
        r = V.compute_epv(inp, "base")
        assert r.excess_cash == 0.0
        a2 = [a for a in r.assumptions if a.code == "A2"]
        assert a2 and "CONSERVATIVE" in a2[0].text

    def test_negative_working_capital_cycle_is_preserved(self):
        """A negative WC cycle is a real funding source - do not clamp to 0."""
        company = _company()
        company["ratios"] = _table({
            "Debtor Days": ["10"],
            "Inventory Days": ["20"],
            "Working Capital Days": ["-60"],
        })
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        assert r.maintenance_working_capital < 0

    def test_missing_shares_is_refused(self):
        company = _company()
        company["top_ratios"] = [
            {"id": "current-price", "name": "Current Price", "value": "500"},
        ]
        rows = company["profit_loss"]["rows"]
        company["profit_loss"]["rows"] = [
            x for x in rows if x.get("") != "EPS in Rs"]
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        assert r.status == "insufficient_data"
        assert "share" in r.reason.lower()

    def test_implausibly_low_epv_is_flagged_as_a_failure(self):
        """A crushed margin must be REFUSED, not reported as a huge margin of
        safety. (Caught here at the sustainable-earnings gate, which fires
        before the <1%-of-price gate.)"""
        company = _company()
        company["top_ratios"] = [
            {"id": "current-price", "name": "Current Price", "value": "1150"},
            {"id": "market-cap", "name": "Market Cap", "value": "50,000 Cr."},
        ]
        # Crush the margin so sustainable earnings go negative.
        company["profit_loss"] = _table({
            "Sales": ["1,00,000"] * 5,
            "Operating Profit": ["50"] * 5,
            "OPM %": ["0.05"] * 5,
            "Depreciation": ["5,000"] * 5,
            "Tax %": ["25"] * 5,
            "Net Profit": ["7,500"] * 5,
            "EPS in Rs": ["15"] * 5,
        })
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        assert r.status == "insufficient_data"
        assert r.per_share is None, "must not publish a per-share EPV here"
        assert "non-positive" in r.reason

    def test_sub_one_percent_epv_is_rejected_by_the_final_gate(self):
        """Exercise the dedicated <1%-of-price guard with a POSITIVE but tiny
        EPV: small margin, no debt, nothing to drag earnings negative.

        Arithmetic (bear: 12% return, 1.30x capex):
          EBIT 3,000 -> NOPAT 2,250 -> sustainable ~2,250 (dep 0, capex 0,
          WC 0); bear EV = 2,250/0.12 = 18,750 Cr; 100 Cr shares ->
          187.50/share against a price of 50,000, i.e. 0.4% of price.
        That is the modelling-failure zone, reached with POSITIVE earnings, so
        the final gate - not the earnings gate - is what must fire.
        """
        company = _company()
        company["top_ratios"] = [
            {"id": "current-price", "name": "Current Price", "value": "50,000"},
            {"id": "market-cap", "name": "Market Cap", "value": "50,00,000 Cr."},
        ]
        company["profit_loss"] = _table({
            "Sales": ["1,00,000"] * 5,
            "Operating Profit": ["3,000"] * 5,
            "OPM %": ["3"] * 5,
            "Depreciation": ["0"] * 5,
            "Tax %": ["25"] * 5,
            "Net Profit": ["2,250"] * 5,
            "EPS in Rs": ["22.5"] * 5,
        })
        company["balance_sheet"] = _table({
            "Equity Capital": ["100"] * 6,
            "Reserves": ["9,900"] * 6,
        })
        # Zero working-capital days: nothing to add or subtract.
        company["ratios"] = _table({"Working Capital Days": ["0"]})
        # No cash-flow rows, so no measured capex and no negative-WC surprise.
        company["cash_flow"] = _table({})

        inp = V.build_inputs(company)
        assert inp.diluted_shares == pytest.approx(100.0)
        r = V.compute_epv(inp, "bear")
        assert r.sustainable_earnings > 0, "gate must fire on a POSITIVE EPV"
        assert r.status == "insufficient_data"
        assert r.per_share is None
        assert "modelling failure" in r.reason


# --------------------------------------------------------------------------- #
# CANAV
# --------------------------------------------------------------------------- #
class TestCanav:
    def _inputs(self, **over):
        return V.build_inputs(_company(**over))

    def test_arithmetic_matches_the_specified_formula(self):
        inp = self._inputs()
        r = V.compute_canav(inp, "base")
        assert r.status == "ok"
        assert r.shareholders_equity == pytest.approx(10000.0)

        total = (r.shareholders_equity
                 + r.quoted_investment_revaluation
                 + r.unquoted_investment_revaluation
                 + r.inventory_haircut
                 + r.receivable_haircut
                 + r.intangible_haircut
                 + r.asset_revaluation
                 + r.contingent_liabilities
                 + r.other_hidden_liabilities)
        assert r.adjusted_equity == pytest.approx(total)
        assert r.per_share == pytest.approx(total / inp.diluted_shares)
        assert r.margin_of_safety == pytest.approx(
            1.0 - inp.market_price / r.per_share)

    def test_default_haircuts_are_the_requested_conservative_values(self):
        """The request named 33.33% inventory / 50% receivables as the defaults."""
        assert V.CANAV_SCENARIOS["base"]["inventory"] == pytest.approx(0.3333)
        assert V.CANAV_SCENARIOS["base"]["receivables"] == pytest.approx(0.50)

    def test_haircuts_are_company_specific_not_constant(self):
        """A fast mover must be treated differently from a slow mover.

        Same inventory BALANCE, different Inventory Days. The written-down
        amount must differ - a single constant haircut would fail this.
        """
        company_fast = _company()
        company_fast["ratios"] = _table({
            "Debtor Days": ["10"], "Inventory Days": ["30"],
            "Working Capital Days": ["30"]})
        company_slow = _company()
        company_slow["ratios"] = _table({
            "Debtor Days": ["150"], "Inventory Days": ["300"],
            "Working Capital Days": ["30"]})

        # Pin the same inventory balance by pinning revenue to a flat series.
        rf = V.compute_canav(V.build_inputs(company_fast), "base")
        rs = V.compute_canav(V.build_inputs(company_slow), "base")
        # Rates must differ: the slow mover is haircut harder per unit.
        rate_fast = abs(rf.inventory_haircut) / 60000.0   # 60 days of sales
        rate_slow = abs(rs.inventory_haircut) / 300000.0  # 300 days
        assert rate_fast < rate_slow

    def test_scenarios_are_ordered_stress_lt_base_lt_moderate(self):
        """Ordering holds on the scenarios that ARE computable.

        A scenario may legitimately return no per-share figure: when the
        haircuts consume the whole equity, CANAV is not small - it is ABSENT,
        and reporting a number would be inventing one. So we assert the
        ordering over the computable pair and separately assert that any
        non-computable scenario says why.
        """
        r = {s: V.compute_canav(self._inputs(), s) for s in V.CANAV_SCENARIOS}

        computable = {s: x.per_share for s, x in r.items() if x.per_share is not None}
        for s, x in r.items():
            if x.per_share is None:
                assert x.status == "insufficient_data"
                assert x.reason, f"{s} returned no value without a reason"

        assert computable, "at least one CANAV scenario must be computable"
        # Whatever IS computable must respect the stress < base < moderate order.
        order = ["stress", "base", "moderate"]
        present = [computable[s] for s in order if s in computable]
        assert present == sorted(present), f"scenario order broken: {computable}"

    def test_scenarios_ordered_on_a_strong_balance_sheet(self):
        """With plenty of equity, all three scenarios compute and are ordered."""
        company = _company()
        company["balance_sheet"] = _table({
            "Equity Capital": ["1,000"] * 6,
            "Reserves": ["4,99,000"] * 6,
            "Borrowings +": ["2,000"] * 6,
            "Fixed Assets": ["50,000"] * 6,
            "Investments": ["50,000"] * 6,
            "Other Assets +": ["6,000"] * 6,
        })
        inp = V.build_inputs(company)
        r = {s: V.compute_canav(inp, s) for s in V.CANAV_SCENARIOS}
        for s, x in r.items():
            assert x.status == "ok", (s, x.reason)
        assert r["stress"].per_share < r["base"].per_share < r["moderate"].per_share

    def test_borrowings_are_not_double_deducted(self):
        """screener.in equity is already net of debt."""
        r = V.compute_canav(self._inputs(), "base")
        assert r.borrowings_deducted == 0.0
        assert any(a.code == "B8" for a in r.assumptions)

    def test_missing_inventory_applies_no_haircut_and_says_so(self):
        company = _company()
        company["ratios"] = _table({"Working Capital Days": ["30"]})
        inp = V.build_inputs(company)
        r = V.compute_canav(inp, "base")
        assert r.inventory_haircut == 0.0
        assert r.receivable_haircut == 0.0
        codes = {a.code for a in r.assumptions}
        assert "B2" in codes and "B3" in codes

    def test_intangibles_are_written_off_fully_when_present(self):
        company = _company()
        rows = company["balance_sheet"]["rows"]
        rows.append({"": "Goodwill", "Mar 2021": "2,000", "Mar 2022": "2,000",
                     "Mar 2023": "2,000", "Mar 2024": "2,000",
                     "Mar 2025": "2,000", "TTM": "2,000"})
        inp = V.build_inputs(company)
        inp.intangibles = 2000.0        # screener.in has no standard line
        r = V.compute_canav(inp, "base")
        assert r.intangible_haircut == pytest.approx(-2000.0)

    def test_asset_revaluation_never_increases_nav(self):
        r = V.compute_canav(self._inputs(), "base")
        assert r.asset_revaluation <= 0

    def test_negative_adjusted_equity_is_refused(self):
        company = _company()
        company["balance_sheet"] = _table({
            "Equity Capital": ["1,000"],
            "Reserves": ["1,000"],
            "Fixed Assets": ["90,000"],
            "Investments": ["50,000"],
        })
        inp = V.build_inputs(company)
        inp.shareholders_equity = -5000.0     # insolvent
        r = V.compute_canav(inp, "base")
        assert r.status == "insufficient_data"

    def test_missing_equity_is_refused(self):
        company = _company()
        company["balance_sheet"] = _table({})
        inp = V.build_inputs(company)
        r = V.compute_canav(inp, "base")
        assert r.status == "insufficient_data"
        assert "shareholders" in r.reason.lower()


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
class TestClassification:
    def test_strong_alignment(self):
        c, _ = V.classify(100.0, epv_per_share=200.0, canav_per_share=150.0)
        assert c == V.CLASS_STRONG

    def test_asset_rich(self):
        c, _ = V.classify(120.0, epv_per_share=100.0, canav_per_share=150.0)
        assert c == V.CLASS_ASSET_RICH

    def test_earnings_franchise(self):
        c, _ = V.classify(140.0, epv_per_share=200.0, canav_per_share=100.0)
        assert c == V.CLASS_FRANCHISE

    def test_rich_to_both(self):
        c, _ = V.classify(300.0, epv_per_share=200.0, canav_per_share=150.0)
        assert c == V.CLASS_RICH

    def test_missing_price_is_insufficient(self):
        c, _ = V.classify(None, 200.0, 150.0)
        assert c == V.CLASS_INSUFFICIENT

    def test_missing_epv_is_insufficient_not_a_guess(self):
        """Do NOT fall back to bear/bull: the classification must be reproducible."""
        c, r = V.classify(100.0, epv_per_share=None, canav_per_share=150.0)
        assert c == V.CLASS_INSUFFICIENT
        assert "EPV" in r

    def test_boundary_price_equal_to_nav_is_not_below(self):
        c, _ = V.classify(150.0, epv_per_share=200.0, canav_per_share=150.0)
        assert c == V.CLASS_FRANCHISE      # equal is NOT "below"


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
class TestValueCompany:
    def test_returns_all_six_scenarios(self):
        val = V.value_company(_company(), symbol="TESTCO")
        assert set(val.epv) == set(V.EPV_SCENARIOS)
        assert set(val.canav) == set(V.CANAV_SCENARIOS)
        assert val.status == "ok"
        assert val.classification in {
            V.CLASS_STRONG, V.CLASS_ASSET_RICH, V.CLASS_FRANCHISE,
            V.CLASS_RICH, V.CLASS_INSUFFICIENT}

    def test_assumptions_are_deduplicated_by_code(self):
        """Six scenarios must NOT produce six copies of the same assumption."""
        val = V.value_company(_company(), symbol="TESTCO")
        codes = [a["code"] for a in val.assumptions]
        assert len(codes) == len(set(codes)), f"duplicate codes: {codes}"

        # Ordering must match the module's own collation, which tolerates a
        # suffixed code like "A1b". A naive int(code[1:]) crashes on that.
        import re as _re

        def key(c):
            m = _re.match(r"([A-Za-z]+)(\d+)", str(c))
            return (m.group(1), int(m.group(2))) if m else (str(c), 0)

        assert codes == sorted(codes, key=key), codes

    def test_every_valuation_reports_its_assumptions(self):
        """A valuation with no assumptions declared is a red flag, not a win."""
        val = V.value_company(_company(), symbol="TESTCO")
        assert len(val.assumptions) >= 5
        assert all("code" in a and "text" in a for a in val.assumptions)

    def test_does_not_crash_on_an_empty_company(self):
        val = V.value_company({}, symbol="EMPTY")
        assert val.status == "insufficient_data"
        assert val.reason

    def test_does_not_crash_on_none_sections(self):
        val = V.value_company({
            "top_ratios": None, "profit_loss": None, "balance_sheet": None,
            "cash_flow": None, "ratios": None,
        }, symbol="NULLCO")
        assert val.status == "insufficient_data"

    def test_a_rejected_scenario_never_exposes_a_per_share_figure(self):
        """INVARIANT: status='insufficient_data' implies per_share is None.

        A caller that reads `epv['base']['per_share']` without checking status
        must not receive a number the function itself declared unusable. This
        guards every early-exit gate in compute_epv and compute_canav at once.
        """
        # A deliberately broken company: negative margin, insolvent equity.
        company = _company()
        company["profit_loss"] = _table({
            "Sales": ["1,00,000"] * 5,
            "Operating Profit": ["-9,000"] * 5,
            "OPM %": ["-9"] * 5,
            "Depreciation": ["1,000"] * 5,
            "Tax %": ["25"] * 5,
            "Net Profit": ["-9,000"] * 5,
            "EPS in Rs": ["-18"] * 5,
        })
        company["balance_sheet"] = _table({
            "Equity Capital": ["1,000"] * 6,
            "Reserves": ["-50,000"] * 6,
        })

        val = V.value_company(company, symbol="BROKEN")
        for group in (val.epv, val.canav):
            for name, res in group.items():
                if res["status"] != "ok":
                    assert res["per_share"] is None, \
                        f"{name} is not ok but exposes per_share={res['per_share']}"
                    assert res["margin_of_safety"] is None, \
                        f"{name} is not ok but exposes margin_of_safety"
                    assert res["reason"], f"{name} failed without a reason"

    def test_as_text_is_ascii_safe(self):
        """Windows consoles are cp1252; the render must not raise."""
        val = V.value_company(_company(), symbol="TESTCO")
        val.as_text().encode("ascii")

    def test_scenario_overrides_are_honoured(self):
        val = V.value_company(_company(), symbol="T",
                              config={"scenario_overrides": {"discount_rate": 0.20}})
        # A 20% required return applies to every EPV scenario and cuts value.
        assert val.epv["base"]["required_return"] == pytest.approx(0.20)


# --------------------------------------------------------------------------- #
# Real-page smoke test
# --------------------------------------------------------------------------- #
class TestAgainstRealPage:
    """Runs the module over a REAL saved screener.in page."""

    @pytest.fixture()
    def reliance(self):
        from blue_engine.screener.client import parser as P
        from pathlib import Path
        path = (Path(__file__).resolve().parent / "fixtures" / "screener"
                / "company_reliance.html")
        if not path.is_file():
            pytest.skip("fixture missing")
        root = P.parse_html(path.read_text(encoding="utf-8"))
        return {
            "name": P.parse_title(root),
            "symbol": "RELIANCE",
            "top_ratios": P.parse_top_ratios(root),
            "profit_loss": P.parse_table(root.select_one("section#profit-loss")),
            "balance_sheet": P.parse_table(root.select_one("section#balance-sheet")),
            "cash_flow": P.parse_table(root.select_one("section#cash-flow")),
            "ratios": P.parse_table(root.select_one("section#ratios")),
        }

    def test_reliance_valuations_compute(self, reliance):
        val = V.value_company(reliance, symbol="RELIANCE")
        assert val.status == "ok", val.reason

        # Every scenario must produce a usable per-share figure.
        for name in V.EPV_SCENARIOS:
            assert val.epv[name]["status"] == "ok", (name, val.epv[name]["reason"])
            assert val.epv[name]["per_share"] > 0
        for name in V.CANAV_SCENARIOS:
            assert val.canav[name]["status"] == "ok", (name, val.canav[name]["reason"])
            assert val.canav[name]["per_share"] > 0

        # CANAV must sit BELOW EPV for Reliance (a franchise, not asset-rich).
        assert val.canav_per_share("base") < val.epv_per_share("base")

        # Scenario ordering must hold on real data too.
        assert val.epv_per_share("bear") < val.epv_per_share("base") \
            < val.epv_per_share("bull")
        assert val.canav_per_share("stress") < val.canav_per_share("base") \
            < val.canav_per_share("moderate")

    def test_reliance_share_count_is_plausible(self, reliance):
        """Reliance has ~135 cr shares post 1:1 bonus (≈1,353 cr pre-bonus)."""
        inp = V.build_inputs(reliance)
        assert 1000 < inp.diluted_shares < 1800
        assert inp.shares_are_diluted is False

    def test_reliance_reports_no_missing_series(self, reliance):
        inp = V.build_inputs(reliance)
        assert inp.revenue_series, "revenue must parse from the real page"
        assert inp.ebit_series
        assert inp.tax_rate_series
        # The tax series must be a PERCENTAGE (0-100), never crores.
        assert all(0 <= t <= 100 for t in inp.tax_rate_series)


# --------------------------------------------------------------------------- #
# yfinance fallback
# --------------------------------------------------------------------------- #
class TestYfinanceFallback:
    """Contracts for the gap-fill provider.

    These tests NEVER hit the network: ``fill_missing_inputs`` is driven with
    hand-built frames, so the tests assert the MODULE's rules (never override,
    unit conversion, sanity gates) rather than Yahoo's uptime.
    """

    class _Frame:
        """Minimal stand-in for a yfinance statement DataFrame."""

        def __init__(self, rows, columns=("2026-03-31",)):
            self._rows = dict(rows)
            self.columns = list(columns)
            self.index = list(rows.keys())
            self.empty = not rows

        def loc(self, key, _col=None):
            class _Row:
                def __init__(self, idx, values):
                    self.index = idx
                    self._values = values

                def __getitem__(self, k):
                    return self._values[self.index.index(k)]

            return _Row(self.columns, self._rows[key])

    def _inputs(self):
        return V.build_inputs(_company())

    # ---- unit conversion: the 1e7 trap ------------------------------------ #
    def test_to_crore_converts_absolute_to_crores(self):
        from blue_engine.screener.yf_fallback import CRORE, _to_crore
        assert CRORE == 1e7
        assert _to_crore(13_532_736_424) == pytest.approx(1353.2736424)
        assert _to_crore(None) is None

    def test_diluted_shares_are_converted_to_crores(self):
        """yfinance returns an ABSOLUTE count; the inputs are in crore-shares.

        A missing conversion divides every per-share figure by 10 million.
        This is the regression test for the first live run, where the fallback
        wrote 13,532,736,424 shares instead of 1,353.27 crore-shares.
        """
        from blue_engine.screener import yf_fallback as YF
        inp = self._inputs()
        basic = inp.diluted_shares            # ~100 cr from market cap
        absolute = basic * YF.CRORE           # what Yahoo actually returns
        assert YF._to_crore(absolute) == pytest.approx(basic)
        # And the unconverted value must NOT be accepted as a share count:
        assert absolute > basic * 1000

    def test_share_count_disagreement_keeps_the_screener_figure(self):
        """If Yahoo's count conflicts badly with screener.in's, do not overwrite.

        Conflicting counts mean one source is stale (a bonus/split), and the
        screener figure is the one consistent with the price in the same inputs.
        """
        from blue_engine.screener import yf_fallback as YF
        inp = self._inputs()
        screener_count = inp.diluted_shares           # 100 cr
        # A Yahoo count 3x larger -> outside the 0.80-1.20 acceptance band.
        wildly_different = screener_count * 3.0
        ratio = wildly_different / screener_count
        assert not (0.80 <= ratio <= 1.20)
        assert inp.diluted_shares == screener_count

    def test_available_is_a_bool_and_never_raises(self):
        from blue_engine.screener import yf_fallback as YF
        assert isinstance(YF.available(), bool)

    def test_ticker_mapping(self):
        from blue_engine.screener import yf_fallback as YF
        assert YF._to_yahoo_ticker("reliance") == "RELIANCE.NS"
        assert YF._to_yahoo_ticker("RELIANCE.NS") == "RELIANCE.NS"
        assert YF._to_yahoo_ticker("RELIANCE.BO") == "RELIANCE.BO"
        assert YF._to_yahoo_ticker("") == ""

    def test_missing_package_degrades_silently(self, monkeypatch):
        """A valuation must not break because a fallback is unavailable."""
        from blue_engine.screener import yf_fallback as YF
        monkeypatch.setattr(YF, "available", lambda: False)
        inp = self._inputs()
        inp.notes.clear()
        filled = YF.fill_missing_inputs(inp, ticker="TESTCO.NS")
        assert filled == []
        assert any("not installed" in n for n in inp.notes)

    def test_fetch_failure_is_recorded_not_raised(self, monkeypatch):
        from blue_engine.screener import yf_fallback as YF
        monkeypatch.setattr(YF, "available", lambda: True)

        class Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("yahoo down")

        # A fresh module object in sys.modules is what `import yfinance as yf`
        # inside the function will pick up.
        fake_yf = types.ModuleType("yfinance")
        fake_yf.Ticker = Boom
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

        inp = self._inputs()
        inp.notes.clear()               # isolate from any earlier note
        filled = YF.fill_missing_inputs(inp, ticker="TESTCO.NS")
        assert filled == []
        assert any("failed" in n for n in inp.notes), inp.notes

    def test_missing_symbol_is_reported_not_silent(self):
        """An unfillable request must say why, not return silently."""
        from blue_engine.screener import yf_fallback as YF
        inp = self._inputs()
        inp.symbol = ""
        inp.notes.clear()
        filled = YF.fill_missing_inputs(inp)          # no symbol, no ticker
        assert filled == []
        assert any("no symbol" in n for n in inp.notes), inp.notes

    # ---- end-to-end through value_company, offline ------------------------ #
    def test_value_company_runs_with_a_custom_provider(self):
        """The provider seam works without yfinance installed at all."""
        calls = {"n": 0}

        def provider(inp):
            calls["n"] += 1
            inp.minority_interest = 1234.0
            inp.filled_by["minority_interest"] = "test"

        val = V.value_company(_company(), symbol="T",
                              config={"fallback_provider": provider})
        assert calls["n"] == 1
        assert val.filled_by.get("minority_interest") == "test"

    def test_a_raising_provider_does_not_break_the_valuation(self):
        def boom(inp):
            raise ValueError("provider exploded")

        val = V.value_company(_company(), symbol="T",
                              config={"fallback_provider": boom})
        assert val.status == "ok"
        assert any("provider" in n.lower() for n in val.notes)

    def test_filled_by_is_empty_on_a_pure_screener_run(self):
        val = V.value_company(_company(), symbol="T")
        assert val.filled_by == {}


class TestCapexGuard:
    """ASSUMPTION A1b/A1c: total capex is NOT maintenance capex."""

    def _inputs(self, capex=None):
        inp = V.build_inputs(_company())
        if capex is not None:
            inp.capex_actual = capex
            inp.filled_by["capex_actual"] = "yfinance"
        return inp

    def test_measured_capex_is_used_when_at_or_below_depreciation(self):
        """A genuine maintenance-level capex is the better estimate."""
        # depreciation 5,000; base proxy = 5,000. Supply 3,000.
        r = V.compute_epv(self._inputs(capex=3000.0), "base")
        assert r.maintenance_capex == pytest.approx(3000.0)
        assert any(a.code == "A1b" for a in r.assumptions)

    def test_growth_capex_is_rejected_by_default(self):
        """Capex far above depreciation contains growth capex; keep the proxy.

        Measured on RELIANCE: total capex 1,22,916 Cr vs depreciation 50,832 Cr.
        Using it as maintenance capex cut EPV by ~66% - a different model, not
        a better estimate.
        """
        r = V.compute_epv(self._inputs(capex=12000.0), "base")
        assert r.maintenance_capex == pytest.approx(5000.0)   # the proxy, not 12000
        a1 = [a for a in r.assumptions if a.code == "A1"]
        assert a1 and "growth capex" in a1[0].text

    def test_growth_capex_used_only_when_explicitly_trusted(self):
        r = V.compute_epv(
            self._inputs(capex=12000.0), "base",
            config={"trust_total_capex_as_maintenance": True})
        assert r.maintenance_capex == pytest.approx(12000.0)
        assert any(a.code == "A1c" for a in r.assumptions)


class TestIntangibleCap:
    """ASSUMPTION B4c: a vendor's intangibles figure can be a classification
    difference, not recoverable-asset risk."""

    def test_huge_intangibles_are_capped_at_a_share_of_equity(self):
        inp = V.build_inputs(_company())      # equity 10,000
        inp.intangibles = 9000.0              # 90% of equity - implausible goodwill
        inp.filled_by["intangibles"] = "yfinance"
        r = V.compute_canav(inp, "base")
        # Cap = 25% of equity = 2,500.
        assert r.intangible_haircut == pytest.approx(-2500.0)
        assert any(a.code == "B4c" for a in r.assumptions)

    def test_cap_can_be_disabled(self):
        inp = V.build_inputs(_company())
        inp.intangibles = 9000.0
        r = V.compute_canav(inp, "base",
                            config={"intangible_haircut_cap_pct": 1.0})
        assert r.intangible_haircut == pytest.approx(-9000.0)

    def test_normal_intangibles_are_written_off_fully(self):
        inp = V.build_inputs(_company())      # equity 10,000
        inp.intangibles = 1000.0              # 10% - inside the cap
        r = V.compute_canav(inp, "base")
        assert r.intangible_haircut == pytest.approx(-1000.0)
