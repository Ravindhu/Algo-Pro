"""Hand-checked cases for execution/lot.py (decision 0022).

Flow stage: Algo router
Status: TEST
"""
import pytest

from quant.execution.lot import lot_quantity, resolve_lot_size


@pytest.mark.parametrize("qty, lot, want", [
    (2500, 1200, 2400),   # the Blue docstring example
    (1200, 1200, 1200),   # exact multiple
    (1199, 1200, 0),      # below one lot
    (1, 1, 1),
    (7, 7, 7),
    (13, 5, 10),
    (0, 1200, 0),
    (-50, 1200, 0),       # never negative when rounding is on
])
def test_lot_quantity_rounds_down(qty, lot, want):
    assert lot_quantity(qty, lot) == want


@pytest.mark.parametrize("qty", [1500, 0, -3])
def test_lot_quantity_zero_or_negative_lot_leaves_quantity_alone_when_positive(qty):
    assert lot_quantity(qty, 0) == max(0, qty)
    assert lot_quantity(qty, -4) == max(0, qty)


def test_override_wins_and_skips_lookup():
    calls = []
    lookup = lambda s, e: calls.append((s, e)) or 50
    assert resolve_lot_size("INFY", "NSE", override=1200, lookup=lookup) == 1200
    assert calls == []


@pytest.mark.parametrize("bad", [0, -1, "abc", [], "-5"])
def test_bad_override_gives_none_without_lookup(bad):
    calls = []
    lookup = lambda s, e: calls.append(1) or 50
    assert resolve_lot_size("INFY", "NSE", override=bad, lookup=lookup) is None
    assert calls == []


def test_float_override_is_truncated_like_blue():
    assert resolve_lot_size("INFY", "NSE", override=2.7) == 2


def test_lookup_value_used_when_no_override():
    assert resolve_lot_size("INFY", "NSE", lookup=lambda s, e: 1200) == 1200


def test_lookup_called_with_symbol_and_exchange():
    seen = []
    resolve_lot_size("INFY", "NSE", lookup=lambda s, e: seen.append((s, e)) or 1)
    assert seen == [("INFY", "NSE")]


@pytest.mark.parametrize("found", [None, 0, -10, "junk"])
def test_lookup_bad_value_gives_none(found):
    assert resolve_lot_size("INFY", "NSE", lookup=lambda s, e: found) is None


def test_lookup_exception_gives_none():
    def broken(s, e):
        raise RuntimeError("db down")
    assert resolve_lot_size("INFY", "NSE", lookup=broken) is None


def test_no_lookup_no_override_gives_none():
    assert resolve_lot_size("INFY", "NSE") is None


@pytest.mark.parametrize("symbol, exchange", [("", "NSE"), ("INFY", ""), (None, "NSE")])
def test_missing_symbol_or_exchange_gives_none(symbol, exchange):
    assert resolve_lot_size(symbol, exchange, lookup=lambda s, e: 1200) is None
