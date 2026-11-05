"""Owner rules (2026-11-06): delivery has no expiry; intraday expires same day;
lot size is SME-only and comes from the broker; SME orders are whole lots only."""
from datetime import date

import pytest

from quant.core.expiry_rules import (
    LotMultipleError, LotSizeUnknown, UnknownProduct, expiry_day, lot_size,
    lot_size_from_broker, must_close_by, sme_order_quantity, validate_lot_multiple,
)

D = date(2026, 11, 6)


class FakeBroker:
    """Stands in for the OpenAlgo client. Returns a canned symbol response."""

    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def symbol(self, *, symbol, exchange, **_):
        self.calls.append((symbol, exchange))
        if self.error:
            raise self.error
        return self.response


# --- expiry ---------------------------------------------------------------

def test_delivery_has_no_expiry():
    assert expiry_day("delivery", D) is None
    assert expiry_day("CNC", D) is None


def test_intraday_expires_same_day():
    assert expiry_day("intraday", D) == D
    assert expiry_day("MIS", D) == D
    assert must_close_by("MIS", D) == D


def test_unknown_product_refused():
    with pytest.raises(UnknownProduct):
        expiry_day("BTST", D)


# --- broker lot size ------------------------------------------------------

def test_broker_lot_size_read_from_lotsize_field():
    broker = FakeBroker({"status": "success", "data": {"symbol": "ABC", "lotsize": 300}})
    assert lot_size_from_broker(broker, "ABC", "NSE_SME") == 300
    assert broker.calls == [("ABC", "NSE_SME")]


def test_broker_lot_size_accepts_flat_response():
    assert lot_size_from_broker(FakeBroker({"lotsize": "250"}), "ABC") == 250


@pytest.mark.parametrize("resp", [{}, {"data": {}}, {"data": {"lotsize": 0}},
                                  {"data": {"lotsize": -5}}, {"data": {"lotsize": "x"}}])
def test_missing_or_bad_broker_lot_refused(resp):
    with pytest.raises(LotSizeUnknown):
        lot_size_from_broker(FakeBroker(resp), "ABC")


def test_broker_error_fails_closed():
    with pytest.raises(LotSizeUnknown):
        lot_size_from_broker(FakeBroker(error=ConnectionError("down")), "ABC")


# --- lot size by segment --------------------------------------------------

def test_non_sme_lot_is_one_share_whatever_broker_says():
    assert lot_size("EQ", None) == 1
    assert lot_size("", 500) == 1


def test_sme_uses_broker_lot():
    assert lot_size("SME", 300) == 300


def test_sme_without_broker_lot_refused():
    with pytest.raises(LotSizeUnknown):
        lot_size("SME", None)
    with pytest.raises(LotSizeUnknown):
        lot_size("SME", 0)


# --- whole-lot orders -----------------------------------------------------

def test_sme_order_is_lots_times_lot_size():
    assert sme_order_quantity(2, 300) == 600


def test_zero_lots_refused():
    with pytest.raises(LotMultipleError):
        sme_order_quantity(0, 300)


@pytest.mark.parametrize("qty", [0, -300, 150, 301])
def test_non_lot_multiple_refused(qty):
    with pytest.raises(LotMultipleError):
        validate_lot_multiple(qty, 300)


def test_lot_multiple_accepted():
    assert validate_lot_multiple(900, 300) == 900


def test_bad_lot_size_refused():
    with pytest.raises(LotMultipleError):
        validate_lot_multiple(10, 0)
