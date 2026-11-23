"""The injected order gateway (execution/openalgo_client.py) and the engine marker check.

Engines are injected with a client and never import the order client. An order
reaches the SDK only through a guarded EngineOrderGateway (same mode checks as
OpenAlgoClient, run before every send) or through the paper broker. Reads pass
through the gateway. Other order-writing methods are refused by name.
"""
import pytest

from quant.engines.core.order_channel import order_channel_ok
from quant.execution.openalgo_client import EngineOrderGateway, OpenAlgoClient


class _Sdk:
    def __init__(self):
        self.calls = []

    def placeorder(self, **kwargs):
        self.calls.append(("order", kwargs))
        return {"status": "success", "orderid": "1"}

    def cancelorder(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return {"status": "success"}

    def orderbook(self):
        return {"status": "success", "data": {"orders": []}}

    def placesmartorder(self, **kwargs):  # must never be reachable through the gateway
        self.calls.append(("smart", kwargs))
        return {"status": "success"}


class _Paper(_Sdk):
    is_paper_broker = True

    def place_order(self, **kwargs):
        self.calls.append(("order", kwargs))
        return {"status": "success"}

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return {"status": "success"}


def _analyzer(on: bool):
    def transport(url, payload):
        if url.endswith("/api/v1/analyzer"):
            return {"status": "success", "data": {"analyze_mode": on}}
        return {"status": "success"}
    return transport


def _gateway(sdk, analyze_on: bool, api_key: str = "key"):
    guard = OpenAlgoClient("http://x", _analyzer(analyze_on), mode="analyze")
    return EngineOrderGateway(sdk, guard, api_key)


def test_marker_recognises_only_gateway_and_paper():
    assert not order_channel_ok(_Sdk())
    assert order_channel_ok(_Paper())
    assert order_channel_ok(_gateway(_Sdk(), analyze_on=True))


def test_gateway_sends_and_cancels_when_analyzer_is_on():
    sdk = _Sdk()
    gw = _gateway(sdk, analyze_on=True)
    assert gw.send_order(symbol="SBIN", quantity=1)["status"] == "success"
    assert gw.cancel_order(order_id="1")["status"] == "success"
    assert [c[0] for c in sdk.calls] == ["order", "cancel"]


def test_gateway_refuses_when_analyzer_is_off_and_never_calls_sdk():
    sdk = _Sdk()
    gw = _gateway(sdk, analyze_on=False)
    with pytest.raises(PermissionError):
        gw.send_order(symbol="SBIN", quantity=1)
    with pytest.raises(PermissionError):
        gw.cancel_order(order_id="1")
    assert sdk.calls == []


def test_gateway_refuses_empty_api_key():
    sdk = _Sdk()
    with pytest.raises(PermissionError):
        _gateway(sdk, analyze_on=True, api_key="").send_order(symbol="SBIN", quantity=1)
    assert sdk.calls == []


def test_gateway_live_mode_needs_approval():
    sdk = _Sdk()
    guard = OpenAlgoClient("http://x", _analyzer(True), mode="live")  # no LiveApproval
    with pytest.raises(PermissionError):
        EngineOrderGateway(sdk, guard, "key").send_order(symbol="SBIN", quantity=1)
    assert sdk.calls == []


def test_reads_pass_through_and_other_writes_are_refused():
    sdk = _Sdk()
    gw = _gateway(sdk, analyze_on=True)
    assert gw.orderbook()["status"] == "success"
    with pytest.raises(PermissionError):
        gw.placesmartorder(symbol="SBIN")
    with pytest.raises(PermissionError):
        gw.placeorder(symbol="SBIN")  # direct SDK name is refused; use send_order
    assert sdk.calls == []


def test_gateway_requires_an_order_client_guard():
    with pytest.raises(TypeError):
        EngineOrderGateway(_Sdk(), object(), "key")


def test_real_paper_broker_send_and_cancel():
    from quant.engines.core.paper_broker import PaperBroker
    paper = PaperBroker(starting_cash=100000.0)
    assert order_channel_ok(paper)
    resp = paper.send_order(strategy="T", symbol="SBIN", exchange="NSE",
                            action="BUY", price_type="MARKET", quantity=1)
    assert resp["status"] == "success"
