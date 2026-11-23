"""The engine order gateway in execution/openalgo_client.py.

Engine code sends orders and cancels only through send_engine_order /
send_engine_cancel, so the call lives in one module. These tests check that
the gateway reaches whichever method the client exposes, and that a client
without one fails loudly instead of silently doing nothing.
"""
import pytest

from quant.execution.openalgo_client import (
    has_engine_cancel_method,
    has_engine_order_method,
    send_engine_cancel,
    send_engine_order,
)


class _SdkStyle:
    def __init__(self):
        self.calls = []

    def placeorder(self, **kwargs):
        self.calls.append(("order", kwargs))
        return {"status": "success", "orderid": "1"}

    def cancelorder(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return {"status": "success"}


class _PaperStyle:
    def __init__(self):
        self.calls = []

    def place_order(self, **kwargs):
        self.calls.append(("order", kwargs))
        return {"status": "success"}

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return {"status": "success"}


def test_sdk_style_client_gets_order_and_cancel():
    client = _SdkStyle()
    assert has_engine_order_method(client) and has_engine_cancel_method(client)
    assert send_engine_order(client, symbol="SBIN", quantity=1)["status"] == "success"
    assert send_engine_cancel(client, order_id="1")["status"] == "success"
    assert [c[0] for c in client.calls] == ["order", "cancel"]


def test_paper_style_client_gets_order_and_cancel():
    client = _PaperStyle()
    assert send_engine_order(client, symbol="SBIN", quantity=1)["status"] == "success"
    assert send_engine_cancel(client, order_id="1")["status"] == "success"
    assert len(client.calls) == 2


def test_client_without_order_method_fails_loudly():
    class Empty:
        pass
    assert not has_engine_order_method(Empty())
    with pytest.raises(RuntimeError):
        send_engine_order(Empty(), symbol="SBIN")
    with pytest.raises(RuntimeError):
        send_engine_cancel(Empty(), order_id="1")
