"""Daily MTF holdings sync with the broker (owner 2026-11-25). Pure.

Flow stage: Risk gate (margin funding), broker reconciliation
Status: PHASE 3 IMPLEMENTED
"""
import pytest

from quant.risk.margin_sync import parse_mtf_rows, reconcile_mtf


def _body(rows, status="success"):
    return {"status": status, "data": rows}


def test_only_mtf_rows_of_the_exchange_are_parsed():
    rows = [
        {"symbol": "INFY", "exchange": "NSE", "product": "MTF", "netqty": 100, "average_price": 1500.0},
        {"symbol": "INFY", "exchange": "NSE", "product": "CNC", "netqty": 50, "average_price": 1500.0},
        {"symbol": "TCS", "exchange": "BSE", "product": "MTF", "netqty": 10, "average_price": 3000.0},
    ]
    out = parse_mtf_rows(_body(rows), "NSE")
    assert out == {"INFY": {"qty": 100.0, "value": 150000.0}}


def test_margin_product_name_counts_as_mtf():
    out = parse_mtf_rows(_body([{"symbol": "INFY", "exchange": "NSE", "product": "MARGIN",
                                 "netqty": 5, "avgprice": 100.0}]), "NSE")
    assert out["INFY"]["qty"] == 5.0


@pytest.mark.parametrize("body", [None, "x", {"status": "error"}, {"status": "success", "data": {}}])
def test_bad_body_raises(body):
    with pytest.raises(ValueError):
        parse_mtf_rows(body)


def test_row_without_symbol_or_with_unreadable_qty_raises():
    with pytest.raises(ValueError):
        parse_mtf_rows(_body([{"exchange": "NSE", "product": "MTF"}]))
    with pytest.raises(ValueError):
        parse_mtf_rows(_body([{"symbol": "INFY", "exchange": "NSE", "product": "MTF", "netqty": "x"}]))


def test_matching_quantities_reconcile_and_value_is_returned():
    ok, issues, value = reconcile_mtf({"INFY": 100}, {"INFY": {"qty": 100.0, "value": 150000.0}})
    assert ok is True and issues == [] and value == pytest.approx(150000.0)


def test_quantity_mismatch_is_an_issue():
    ok, issues, _ = reconcile_mtf({"INFY": 100}, {"INFY": {"qty": 90.0, "value": 135000.0}})
    assert ok is False and issues == ["INFY: ours 100, broker 90"]


def test_position_missing_at_broker_is_an_issue():
    ok, issues, value = reconcile_mtf({"INFY": 100}, {})
    assert ok is False and "INFY: ours 100, broker 0" in issues and value == 0.0


def test_extra_mtf_at_broker_is_an_issue():
    ok, issues, _ = reconcile_mtf({}, {"TCS": {"qty": 10.0, "value": 30000.0}})
    assert ok is False and issues == ["TCS: ours 0, broker 10"]
