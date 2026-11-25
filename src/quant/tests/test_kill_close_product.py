"""Kill-switch closes carry the position's product (CNC or MIS). Never a guessed product.

Flow stage: Kill switch
Status: PHASE 1 IMPLEMENTED (pure function; no broker call)
Regression: before this change every kill close was built with the default MIS product.
"""
import pytest

from quant.risk.kill_close import kill_exit_intents


def test_close_carries_the_given_cnc_product():
    intents, skipped = kill_exit_intents({"TCS": 5}, {"TCS": 100.0}, [], product="CNC")
    assert skipped == []
    assert len(intents) == 1
    assert intents[0].product == "CNC"
    assert intents[0].side == "SELL" and intents[0].qty == 5


def test_close_carries_mis_when_given_mis():
    intents, _ = kill_exit_intents({"TCS": -3}, {"TCS": 100.0}, [], product="mis")
    assert intents[0].product == "MIS"
    assert intents[0].side == "BUY"


def test_no_product_means_no_close_regression():
    # Regression: the old code built this close as MIS. A close with an unknown product is not sent.
    intents, skipped = kill_exit_intents({"TCS": 5}, {"TCS": 100.0}, [])
    assert intents == []
    assert skipped and "product unknown" in skipped[0][1]


@pytest.mark.parametrize("bad", ["NRML", "SELL"])
def test_unknown_product_refused(bad):
    with pytest.raises(ValueError, match="MIS, CNC or MTF"):
        kill_exit_intents({"TCS": 5}, {"TCS": 100.0}, [], product=bad)


def test_margin_product_accepted_for_kill_close():
    # owner 2026-11-25: MTF (margin-funded delivery) is a valid kill-close product
    intents, _ = kill_exit_intents({"TCS": 5}, {"TCS": 100.0}, [], product="MTF")
    assert len(intents) == 1 and intents[0].product == "MTF"


def test_no_reference_price_still_skipped_with_product():
    intents, skipped = kill_exit_intents({"TCS": 5}, {}, [], product="CNC")
    assert intents == [] and skipped[0][1] == "no reference price"
