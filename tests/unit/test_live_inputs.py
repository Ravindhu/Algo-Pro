"""Read-only live inputs (funds, positions, open orders). Every read error fails closed."""
from quant.core.types import TradeIntent
from quant.execution.live_inputs import LiveReaders, has_open_order, position_qty


class FakeReader:
    def __init__(self, funds=None, positions=None, orders=None, fail=False):
        self._funds, self._pos, self._orders, self._fail = funds, positions, orders, fail

    def _reply(self, data):
        if self._fail:
            raise ConnectionError("down")
        return {"status": "success", "data": data}

    def funds(self, key):
        return self._reply({"availablecash": self._funds})

    def positionbook(self, key):
        return self._reply(self._pos)

    def orderbook(self, key):
        return self._reply({"orders": self._orders, "statistics": {}})


def sell(qty=100):
    return TradeIntent("X1", "S1", "INFY", "SELL", qty, 1500.0, kind="exit")


def buy(qty=100):
    return TradeIntent("X2", "S1", "INFY", "BUY", qty, 1500.0, kind="exit")


def test_available_funds_parsed():
    assert LiveReaders(FakeReader(funds="65000.50"), "k").funds() == 65000.50


def test_funds_error_gives_zero_so_entries_are_refused():
    assert LiveReaders(FakeReader(fail=True), "k").funds() == 0.0


def test_position_sums_signed_quantity_for_symbol():
    rows = [{"symbol": "INFY", "quantity": 60}, {"symbol": "INFY", "quantity": 40}, {"symbol": "TCS", "quantity": 9}]
    assert position_qty(FakeReader(positions=rows), "k", "INFY") == 100


def test_open_order_detected_and_closed_ones_ignored():
    orders = [{"symbol": "INFY", "action": "SELL", "order_status": "complete"}]
    assert has_open_order(FakeReader(orders=orders), "k", "INFY", "SELL") is False
    orders.append({"symbol": "INFY", "action": "SELL", "order_status": "open"})
    assert has_open_order(FakeReader(orders=orders), "k", "INFY", "SELL") is True


def test_not_placed_is_false_on_read_error_so_no_retry():
    assert LiveReaders(FakeReader(fail=True), "k").not_placed(sell()) is False


def test_still_open_for_long_exit_needs_enough_shares():
    r = LiveReaders(FakeReader(positions=[{"symbol": "INFY", "quantity": 100}]), "k")
    assert r.still_open(sell(100)) is True
    assert r.still_open(sell(101)) is False


def test_still_open_for_short_exit_uses_negative_quantity():
    r = LiveReaders(FakeReader(positions=[{"symbol": "INFY", "quantity": -100}]), "k")
    assert r.still_open(buy(100)) is True


def test_still_open_is_false_on_read_error():
    assert LiveReaders(FakeReader(fail=True), "k").still_open(sell()) is False


def test_readers_need_a_key():
    import pytest
    with pytest.raises(ValueError):
        LiveReaders(FakeReader(), "")
