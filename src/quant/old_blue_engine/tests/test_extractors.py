"""
test_extractors.py - tests for ``screener/client/extractors.py``.

These pin the REAL screener.in labels. The point of the module is that guessing
a label is how a valuation silently loses a figure, so most tests here assert a
label match works AND that the wrong row cannot be picked up by accident.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.screener.client import extractors as EX  # noqa: E402
from blue_engine.screener.client.parser import parse_html, parse_table  # noqa: E402


# --------------------------------------------------------------------------- #
# The labels are the REAL ones, verbatim from the live page
# --------------------------------------------------------------------------- #
CASHFLOW_LABELS = {
    "Cash from Operating Activity +": ["1,00,000", "1,10,000", "1,92,113"],
    "Cash from Investing Activity +": ["-80,000", "-90,000", "-101,089"],
    "Cash from Financing Activity +": ["-20,000", "-25,000", "-51,549"],
    "Net Cash Flow": ["0", "-5,000", "39,475"],
    "Free Cash Flow": ["70,000", "75,000", "70,023"],
}

BALANCE_LABELS = {
    "Equity Capital": ["100", "100", "100"],
    "Reserves": ["9,00,000", "9,50,000", "9,04,030"],
    "Borrowings +": ["3,00,000", "3,50,000", "4,02,962"],
    "Other Liabilities +": ["5,00,000", "5,50,000", "6,00,000"],
    "Total Liabilities": ["17,00,000", "18,50,000", "21,77,546"],
    "Fixed Assets +": ["5,00,000", "5,50,000", "5,80,000"],
    "CWIP": ["50,000", "60,000", "70,000"],
    "Investments": ["2,00,000", "2,20,000", "2,48,332"],
    "Other Assets +": ["8,50,000", "9,20,000", "9,79,214"],
    "Total Assets": ["17,00,000", "18,50,000", "21,77,546"],
}
# Component check: 5,80,000 + 70,000 + 2,48,332 + 9,79,214 = 18,77,546, which is
# NOT the 21,77,546 total above. The real page DOES balance; this fixture is
# deliberately built so that a test which needs consistency constructs it
# explicitly. See ``_balanced_company``.


def _table(rows, headers=None):
    if headers is None:
        headers = ["", "Mar 2023", "Mar 2024", "Mar 2025"]
    out = []
    for label, values in rows.items():
        row = {headers[0]: label}
        for i, v in enumerate(values):
            row[headers[i + 1]] = v
        out.append(row)
    return {"headers": headers, "rows": out}


def _company(**over):
    base = {
        "profit_loss": _table({
            "Sales": ["1,00,000", "1,10,000", "1,12,305"],
            "Operating Profit": ["15,000", "16,500", "16,846"],
            "OPM %": ["15", "15", "15"],
            "Other Income": ["2,000", "2,200", "2,400"],
            "Interest": ["1,000", "1,100", "1,200"],
            "Depreciation": ["5,000", "5,500", "6,000"],
            "Profit before tax": ["11,000", "12,100", "12,046"],
            "Tax %": ["25", "25", "24"],
            "Net Profit": ["8,250", "9,075", "9,155"],
            "EPS in Rs": ["6", "6.6", "6.8"],
        }),
        "balance_sheet": _table(BALANCE_LABELS),
        "cash_flow": _table(CASHFLOW_LABELS),
        "ratios": _table({
            "Debtor Days": ["20", "22", "25"],
            "Inventory Days": ["40", "45", "50"],
            "Days Payable": ["60", "65", "70"],
            "Working Capital Days": ["10", "12", "15"],
        }),
        "top_ratios": [
            {"id": "current-price", "name": "Current Price", "value": "1,000"},
            {"id": "market-cap", "name": "Market Cap", "value": "1,00,000 Cr."},
        ],
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Row lookup
# --------------------------------------------------------------------------- #
class TestFindRow:
    def test_exact_match_strips_the_dangling_plus(self):
        """The page writes 'Borrowings +'. The '+' is punctuation, not the name.

        The fixture uses the REAL label, with the '+', so this test fails if the
        stripping is removed.
        """
        t = _table({"Borrowings +": ["3,00,000", "3,50,000", "4,02,962"]})
        row = EX.find_row(t, EX.BS_BORROWINGS)
        assert row is not None
        assert row[""] == "Borrowings +"
        assert EX.row_latest(t, EX.BS_BORROWINGS) == pytest.approx(402962.0)

    def test_tax_pct_is_not_fooled_by_profit_before_tax(self):
        """THE critical label test.

        'Profit before tax' contains the substring 'tax' and holds CRORES.
        'Tax %' holds a percentage. On the REAL page 'Profit before tax' comes
        FIRST, so a substring lookup for 'tax' returns it - putting a crore
        figure into a tax rate. The row order below matches the real page.
        """
        t = _table({
            "Profit before tax": ["11,000", "12,100", "12,046"],
            "Tax %": ["25", "25", "24"],
        })
        # Exact match gets the percentage, regardless of row order.
        assert EX.row_latest(t, EX.PL_TAX_PCT) == 24.0
        # A loose substring match would have returned the crore figure - this
        # assertion documents exactly why the default is exact matching.
        assert EX.row_latest(t, ("tax",), exact=False) == 12046.0

    def test_real_page_lookup_finds_tax_pct_not_pbt(self):
        """Same check against the real saved page, where PBT appears FIRST."""
        path = (Path(__file__).resolve().parent / "fixtures" / "screener"
                / "company_reliance.html")
        if not path.is_file():
            pytest.skip("fixture missing")
        root = parse_html(path.read_text(encoding="utf-8"))
        pl = parse_table(root.select_one("section#profit-loss"))
        tax = EX.row_latest(pl, EX.PL_TAX_PCT)
        assert tax is not None
        assert 0 <= tax <= 100, f"tax rate must be a %, got {tax}"

    def test_cash_from_investing_activity_matches_real_label(self):
        """Regression: the old label 'Investing Activity' matched NOTHING.

        It only appeared to work because the unit fixture used the short form.
        The live page says 'Cash from Investing Activity +'.
        """
        t = _table(CASHFLOW_LABELS)
        assert EX.row_latest(t, EX.CF_INVESTING) == -101089.0
        # The bare short label must NOT match on its own...
        assert EX.row_latest(t, ("investing activity",)) is None

    def test_singular_plural_stem_matches_both_vendor_labels(self):
        """THE bug: HDFCBANK writes 'Borrowing', ITC writes 'Borrowings +'.

        A stem of 'borrowing' must catch BOTH. Plain equality caught neither
        once the '+' was stripped, because 'borrowing' != 'borrowings'.
        """
        for label in ("Borrowing", "Borrowings +", "Borrowings", "borrowing"):
            t = _table({label: ["3,00,000", "3,50,000", "4,02,962"]})
            assert EX.row_latest(t, EX.BS_BORROWINGS) == pytest.approx(402962.0), \
                f"failed for real-world label {label!r}"

    def test_plural_tolerance_does_not_overreach(self):
        """Singular/plural tolerance must not become a substring match."""
        t = _table({"Reserves And Surplus": ["1"]})
        assert EX.row_latest(t, EX.BS_RESERVES) is None
        t2 = _table({"Borrowing Costs": ["1"]})
        assert EX.row_latest(t2, EX.BS_BORROWINGS) is None

    def test_missing_label_returns_none(self):
        t = _table(BALANCE_LABELS)
        assert EX.find_row(t, ("Nonexistent Row",)) is None

    def test_none_table_is_safe(self):
        assert EX.find_row(None, EX.BS_BORROWINGS) is None
        assert EX.row_latest(None, EX.BS_BORROWINGS) is None
        assert EX.row_series(None, EX.BS_BORROWINGS) == []


# --------------------------------------------------------------------------- #
# Cash flow / capex
# --------------------------------------------------------------------------- #
class TestCapex:
    def test_capex_recovered_as_cfo_minus_fcf(self):
        """Exact: screener.in defines FCF as CFO minus capex."""
        capex = EX.capex_from_cashflow(_company())
        assert capex == pytest.approx(192113.0 - 70023.0)

    def test_positive_investing_figure_is_rejected(self):
        """A net investing INFLOW proves the line contains disposals.

        Using it as capex would return a negative capex and inflate EPV.
        """
        company = _company()
        company["cash_flow"] = _table({
            "Cash from Investing Activity +": ["+50,000"],
            "Cash from Operating Activity +": ["1,00,000"],
            # No Free Cash Flow row -> the exact path is unavailable.
        })
        assert EX.capex_from_cashflow(company) is None

    def test_falls_back_to_negative_investing(self):
        company = _company()
        company["cash_flow"] = _table({
            "Cash from Investing Activity +": ["-80,000"],
        })
        assert EX.capex_from_cashflow(company) == pytest.approx(80000.0)

    def test_no_cashflow_table_is_none(self):
        assert EX.capex_from_cashflow({"cash_flow": None}) is None

    def test_series_have_all_five_lines(self):
        cf = EX.cashflow_series(_company())
        assert set(cf) == {"operating", "investing", "financing",
                           "net_cash_flow", "free_cash_flow"}
        assert cf["operating"][-1] == pytest.approx(192113.0)


# --------------------------------------------------------------------------- #
# Balance sheet
# --------------------------------------------------------------------------- #
class TestBalanceSheet:
    def test_snapshot_reads_every_printed_line(self):
        snap = EX.balance_sheet_snapshot(_company())
        assert snap["equity_capital"] == pytest.approx(100.0)
        assert snap["reserves"] == pytest.approx(904030.0)
        assert snap["borrowings"] == pytest.approx(402962.0)
        assert snap["investments"] == pytest.approx(248332.0)
        assert snap["total_assets"] == pytest.approx(2177546.0)
        # Signed rows carry the '+', so these three are the ones that prove the
        # dangling-plus strip works on every printed label, not just one.
        assert snap["other_liabilities"] == pytest.approx(600000.0)
        assert snap["fixed_assets"] == pytest.approx(580000.0)
        assert snap["other_assets"] == pytest.approx(979214.0)

    def test_unavailable_lines_are_none_not_zero(self):
        """These are NOT on the page. Returning 0 would be a fabrication."""
        snap = EX.balance_sheet_snapshot(_company())
        assert snap["cash_and_equivalents"] is None
        assert snap["minority_interest"] is None
        assert snap["intangibles"] is None
        assert snap["contingent_liabilities"] is None

    def test_balances_when_the_page_is_consistent(self):
        """A genuinely consistent page must pass BOTH checks.

        The shared fixture is deliberately inconsistent on the component sum
        (see BALANCE_LABELS), so this test builds a page that really balances.
        """
        company = _company()
        company["balance_sheet"] = _table({
            "Equity Capital": ["100"],
            "Reserves": ["9,04,030"],
            "Borrowings +": ["4,02,962"],
            "Other Liabilities +": ["6,00,000"],
            "Total Liabilities": ["19,07,092"],
            "Fixed Assets +": ["5,80,000"],
            "CWIP": ["70,000"],
            "Investments": ["2,48,332"],
            "Other Assets +": ["10,08,760"],
            "Total Assets": ["19,07,092"],
        })
        bc = EX.balance_sheet_balances(company)
        assert bc["balanced"] is True, bc
        assert bc["checks"]["assets_eq_liabilities"] is True
        assert bc["checks"]["assets_eq_components"] is True

    def test_detects_an_inconsistent_page(self):
        """A mis-read label must surface as 'not balanced', not pass silently."""
        company = _company()
        company["balance_sheet"] = _table({
            "Equity Capital": ["100"],
            "Reserves": ["9,00,000"],
            "Total Assets": ["21,77,546"],
            "Total Liabilities": ["9,00,100"],        # deliberately wrong
        })
        bc = EX.balance_sheet_balances(company)
        assert bc["checks"]["assets_eq_liabilities"] is False
        assert bc["balanced"] is False

    def test_equity_is_capital_plus_reserves(self):
        assert EX.equity_from_balance_sheet(_company()) == pytest.approx(904130.0)


# --------------------------------------------------------------------------- #
# Working capital
# --------------------------------------------------------------------------- #
class TestWorkingCapital:
    def test_components_are_derived_from_the_ratios(self):
        wc = EX.net_working_assets(_company())
        daily = 112305.0 / 365.0
        assert wc["inventory"] == pytest.approx(50 * daily)
        assert wc["receivables"] == pytest.approx(25 * daily)
        assert wc["payables"] == pytest.approx(70 * daily)
        assert wc["working_capital"] == pytest.approx(15 * daily)

    def test_absurd_day_counts_are_refused(self):
        company = _company()
        company["ratios"] = _table({
            "Inventory Days": ["99999"], "Debtor Days": ["-50000"],
        })
        wc = EX.net_working_assets(company)
        assert wc["inventory"] is None
        assert wc["receivables"] is None

    def test_missing_ratio_yields_none(self):
        company = _company()
        company["ratios"] = _table({"Working Capital Days": ["15"]})
        wc = EX.net_working_assets(company)
        assert wc["inventory"] is None
        assert wc["working_capital"] is not None


# --------------------------------------------------------------------------- #
# Market
# --------------------------------------------------------------------------- #
class TestMarket:
    def test_shares_from_market_cap(self):
        m = EX.market_snapshot(_company())
        assert m["price"] == pytest.approx(1000.0)
        assert m["market_cap"] == pytest.approx(100000.0)
        assert m["shares_from_market_cap"] == pytest.approx(100.0)

    def test_prefers_the_market_cap_count(self):
        """It is the count consistent with the price in the same snapshot."""
        m = EX.market_snapshot(_company())
        assert m["shares_source"] == "market_cap/price"

    def test_disagreement_is_reported(self):
        m = EX.market_snapshot(_company())
        # EPS implies 9155/6.8 = 1346 cr, mcap implies 100 cr -> big gap.
        assert m["shares_disagreement_pct"] is not None
        assert m["shares_disagreement_pct"] > 0.15

    def test_missing_price_and_cap(self):
        company = _company()
        company["top_ratios"] = []
        m = EX.market_snapshot(company)
        assert m["price"] is None
        assert m["market_cap"] is None


# --------------------------------------------------------------------------- #
# Statement depth + bundle
# --------------------------------------------------------------------------- #
class TestBundle:
    def test_statement_years_counts_annual_columns(self):
        years = EX.statement_years(_company())
        assert years["profit_loss"] == 3
        assert years["balance_sheet"] == 3

    def test_ttm_is_not_counted_as_an_annual_year(self):
        company = _company()
        company["profit_loss"] = _table(
            {"Sales": ["1,00,000", "1,10,000", "1,12,305", "99,999"]},
            headers=["", "Mar 2023", "Mar 2024", "Mar 2025", "TTM"])
        assert EX.statement_years(company)["profit_loss"] == 3

    def test_bundle_lists_what_is_unavailable(self):
        bundle = EX.extraction_bundle(_company())
        assert "unavailable" in bundle
        blob = " ".join(bundle["unavailable"]).lower()
        assert "contingent" in blob
        assert "minority" in blob
        assert "intangibles" in blob
        assert "cash" in blob

    def test_bundle_marks_contingent_as_gated_when_absent(self):
        """On an anonymous fetch the row is GATED, not absent.

        Reporting a bare None would let a caller read "we could not see it" as
        "the company has none" - the one misreading that costs money.
        """
        bundle = EX.extraction_bundle(_company())
        assert bundle["contingent_liabilities"] is None
        assert "not-visible" in bundle["contingent_source"]
        assert "LOGGED-IN" in " ".join(bundle["unavailable"])

    def test_bundle_reports_source_when_present(self):
        company = _company()
        company["contingent_liabilities"] = "1,234"
        bundle = EX.extraction_bundle(company)
        assert bundle["contingent_liabilities"] == pytest.approx(1234.0)
        assert bundle["contingent_source"] == "screener:logged-in-ratios"

    def test_bundle_never_raises_on_an_empty_company(self):
        bundle = EX.extraction_bundle({})
        assert isinstance(bundle, dict)
        assert bundle["capex"] is None


# --------------------------------------------------------------------------- #
# Real live page
# --------------------------------------------------------------------------- #
class TestAgainstRealPage:
    @pytest.fixture()
    def reliance(self):
        path = (Path(__file__).resolve().parent / "fixtures" / "screener"
                / "company_reliance.html")
        if not path.is_file():
            pytest.skip("fixture missing")
        root = parse_html(path.read_text(encoding="utf-8"))
        from blue_engine.screener.client.parser import parse_top_ratios
        return {
            "top_ratios": parse_top_ratios(root),
            "profit_loss": parse_table(root.select_one("section#profit-loss")),
            "balance_sheet": parse_table(root.select_one("section#balance-sheet")),
            "cash_flow": parse_table(root.select_one("section#cash-flow")),
            "ratios": parse_table(root.select_one("section#ratios")),
        }

    def test_real_page_balance_sheet_balances(self, reliance):
        """The strongest single integrity check on a page we did not write."""
        bc = EX.balance_sheet_balances(reliance)
        assert bc["balanced"] is True, bc
        assert bc["checks"]["assets_eq_liabilities"] is True
        assert bc["checks"]["assets_eq_components"] is True

    def test_real_page_has_12_annual_columns(self, reliance):
        years = EX.statement_years(reliance)
        assert years["profit_loss"] >= 10
        assert years["balance_sheet"] >= 10

    def test_real_page_capex_is_positive_and_matches_cfo_minus_fcf(self, reliance):
        cf = EX.cashflow_series(reliance)
        cfo = [x for x in cf["operating"] if x is not None][-1]
        fcf = [x for x in cf["free_cash_flow"] if x is not None][-1]
        capex = EX.capex_from_cashflow(reliance)
        assert capex is not None and capex > 0
        assert capex == pytest.approx(cfo - fcf)

    def test_real_page_borrowings_use_the_stem_match(self, reliance):
        snap = EX.balance_sheet_snapshot(reliance)
        assert snap["borrowings"] is not None
        assert snap["borrowings"] > 0
        # And the '+'-suffixed signed rows resolve too.
        assert snap["fixed_assets"] is not None
        assert snap["other_assets"] is not None


# --------------------------------------------------------------------------- #
# Contingent liabilities (logged-in only)
# --------------------------------------------------------------------------- #
class TestContingentLiabilities:
    """The row is served only to a logged-in screener.in session.

    Verified: four greps of the anonymous page across RELIANCE, ITC and
    KPITTECH found zero hits for 'contingent', 'Cont. Liab' and ten other
    spellings - the substring 'cont' does not appear anywhere on the page. So
    matching here is deliberately tolerant, unlike every other label in the
    module, because the exact label cannot be observed anonymously.
    """

    def test_returns_none_on_an_anonymous_page(self):
        """Expected outcome - and NOT to be read as 'the company has none'."""
        assert EX.contingent_liabilities(_company()) is None

    @pytest.mark.parametrize("label", [
        "Contingent Liabilities",
        "Contingent Liab.",
        "Cont. Liab",
        "Cont Liab",
        "Cont.Liab",
        "contingent liabilities",
    ])
    def test_matches_every_plausible_abbreviation(self, label):
        company = _company()
        company["ratios"] = _table({label: ["1,00,000", "1,20,000", "1,34,567"]})
        assert EX.contingent_liabilities(company) == pytest.approx(134567.0)

    def test_reads_the_latest_period(self):
        company = _company()
        company["balance_sheet"] = _table({
            "Cont. Liab": ["10,000", "20,000", "34,567"],
        })
        assert EX.contingent_liabilities(company) == pytest.approx(34567.0)

    def test_accepts_a_preparsed_key(self):
        company = _company()
        company["contingent_liabilities"] = "5,000 Cr."
        assert EX.contingent_liabilities(company) == pytest.approx(5000.0)

    def test_unparseable_value_is_rejected_not_zeroed(self):
        """A label whose value will not parse must not become 0.

        Returning 0 would tell EPV 'there is no contingent liability' when the
        truth is 'the figure could not be read'.
        """
        company = _company()
        company["ratios"] = _table({"Cont. Liab": ["-", "N/A", "n/a"]})
        assert EX.contingent_liabilities(company) is None

    def test_negative_value_is_rejected(self):
        company = _company()
        company["ratios"] = _table({"Cont. Liab": ["-5,000"]})
        assert EX.contingent_liabilities(company) is None

    def test_does_not_false_positive_on_unrelated_cont_rows(self):
        """Tolerance must not become a catch-all.

        Real page rows that contain 'cont' but are NOT contingent liabilities.
        """
        company = _company()
        company["ratios"] = _table({
            "Contribution Margin": ["40"],
            "Contract Assets": ["100"],
            "Control Premium": ["5"],
        })
        assert EX.contingent_liabilities(company) is None

    def test_probe_returns_empty_on_an_anonymous_page(self):
        """The probe finding nothing IS the proof the row is gated."""
        path = (Path(__file__).resolve().parent / "fixtures" / "screener"
                / "company_reliance.html")
        if not path.is_file():
            pytest.skip("fixture missing")
        html = path.read_text(encoding="utf-8")
        assert EX.contingent_liabilities_label_probe(html) == []

    def test_probe_finds_the_label_when_present(self):
        html = """
        <section id="ratios"><table>
          <tr><td>Debtor Days</td><td>40</td></tr>
          <tr><td>Cont. Liab</td><td>1,34,567</td></tr>
        </table></section>
        """
        found = EX.contingent_liabilities_label_probe(html)
        assert found, "probe must surface the real label"
        assert any("Cont" in f for f in found)


class TestContingentInValuation:
    """How the valuation treats a gated vs a measured contingent liability."""

    def test_absent_row_is_zero_with_an_honest_assumption(self):
        from blue_engine.screener import valuations as V
        inp = V.build_inputs(_company_for_valuation())
        r = V.compute_epv(inp, "base")
        assert r.debt_like == 0.0
        a11 = [a for a in r.assumptions if a.code == "A11"]
        assert a11, "the 0 default must be declared"
        assert "not visible" in a11[0].text.lower() or \
               "NOT 'the company has none'" in a11[0].text

    def test_measured_row_is_deducted(self):
        from blue_engine.screener import valuations as V
        company = _company_for_valuation()
        company["contingent_liabilities"] = "50,000"
        inp = V.build_inputs(company)
        r = V.compute_epv(inp, "base")
        assert r.debt_like == pytest.approx(50000.0)
        assert any(a.code == "A11b" for a in r.assumptions)


def _company_for_valuation():
    """A minimal complete company for the valuation-level tests above.

    Uses the 6-column header so six values per row are in range - the shared
    ``_table`` default only has three period columns.
    """
    headers = ["", "Mar 2021", "Mar 2022", "Mar 2023", "Mar 2024",
               "Mar 2025", "TTM"]

    def t(rows):
        return _table(rows, headers=headers)

    return {
        "top_ratios": [
            {"id": "current-price", "name": "Current Price", "value": "500"},
            {"id": "market-cap", "name": "Market Cap", "value": "50,000 Cr."},
        ],
        "profit_loss": t({
            "Sales": ["1,00,000"] * 6,
            "Operating Profit": ["15,000"] * 6,
            "OPM %": ["15"] * 6,
            "Depreciation": ["5,000"] * 6,
            "Tax %": ["25"] * 6,
            "Net Profit": ["7,500"] * 6,
            "EPS in Rs": ["15"] * 6,
        }),
        "balance_sheet": t({
            "Equity Capital": ["100"] * 6,
            "Reserves": ["9,900"] * 6,
            "Borrowings +": ["2,000"] * 6,
        }),
        "cash_flow": t({
            "Cash from Operating Activity +": ["10,000"] * 6,
            "Free Cash Flow": ["6,000"] * 6,
        }),
        "ratios": t({"Working Capital Days": ["30"] * 6}),
    }
