"""Hand-checked tests for src/quant/data/screen_rules.py (no Blue needed).

Numbers worked out by hand; see the comments on each case.
"""
import pytest

from quant.data.screen_rules import (
    FAIL, PASS, UNKNOWN, Rule, Screener, default_rules, to_number,
)


@pytest.mark.parametrize("cell,expected", [
    ("1,23,456 Cr.", 123456.0),   # crores as written
    ("50 Lakh", 0.5),             # 50 x 0.01 crore
    ("2.5 bn", 250.0),            # 2.5 x 100 crore
    ("12%", 12.0),                # % is decorative
    ("₹ 300", 300.0),             # rupee sign ignored
    ("-", None),                  # blank token is None, never 0
    ("N/A", None),
    (True, None),                 # bool is not a number
    ("abc", None),
    (0, 0.0),                     # a real zero is zero
])
def test_to_number_cells(cell, expected):
    assert to_number(cell) == expected


def test_market_cap_200_exactly_is_excluded():
    # Rule is strict: > 200 Cr. 200 is FAIL.
    r = default_rules()[0].evaluate({"market_cap": "200"})
    assert r.outcome == FAIL


def test_debt_equity_1_exactly_is_excluded():
    # Rule is strict: < 1. 1.0 is FAIL.
    r = default_rules()[1].evaluate({"debt_equity": "1.0"})
    assert r.outcome == FAIL


def test_missing_debt_is_not_taken_as_zero():
    # A blank debt cell must not pass "< 1". Default policy is fail.
    r = default_rules()[1].evaluate({"debt_equity": "-"})
    assert r.outcome == FAIL and r.value is None


def test_unknown_policy_is_reported_separately():
    r = Rule(metric="x", op=">", threshold=1, on_missing="unknown").evaluate({})
    assert r.outcome == UNKNOWN


def test_bad_on_missing_refused():
    with pytest.raises(ValueError):
        Rule(metric="x", op=">", threshold=1, on_missing="skip")


def test_bank_excluded_before_debt_rule():
    s = Screener.from_config({})  # default sector deny list
    (res,) = s.evaluate([{"symbol": "HDFC", "sector": "Private Banks",
                          "metrics": {"market_cap": "900000", "debt_equity": "9"}}])
    assert res.selected is False
    assert "excluded" in res.reason()
    assert res.rules == []  # debt rule never ran for a bank


def test_selected_when_both_rules_pass():
    s = Screener.from_config({})
    (res,) = s.evaluate([{"symbol": "aaa", "sector": "Chemicals",
                          "metrics": {"market_cap": "5,000 Cr.", "debt_equity": "0.4"}}])
    assert res.symbol == "AAA"
    assert res.selected is True
    assert res.reason() == "passed all rules"


def test_one_failed_rule_rejects_when_all_required():
    s = Screener.from_config({})
    (res,) = s.evaluate([{"symbol": "BBB", "sector": "Chemicals",
                          "metrics": {"market_cap": "150 Cr", "debt_equity": "0.2"}}])
    assert res.selected is False
    assert [r.metric for r in res.failed_rules] == ["market_cap"]


def test_empty_sector_list_turns_gate_off():
    s = Screener.from_config({"exclude_sectors": []})
    (res,) = s.evaluate([{"symbol": "HDFC", "sector": "Banks",
                          "metrics": {"market_cap": "900000", "debt_equity": "0.5"}}])
    assert res.selected is True


def test_empty_rule_list_refused():
    with pytest.raises(ValueError):
        Screener([])
