"""The engine order gateway in execution/openalgo_client.py.

Engine code sends orders only through send_engine_order / send_engine_cancel.
An order reaches the SDK only through a guarded EngineOrderGateway (same mode
checks as OpenAlgoClient, run before every send) or through the paper broker.
Any other client is refused.
"""
import pytest

from quant.execution.openalgo_client import (
    EngineOrderGateway,
    OpenAlgoClient,
    has_engine_cancel_method,
    has_engine_order_method,
    send_engine_cancel,
    send_engine_order,
)


class _Sdk:
    def __init__(self):
        self.calls = []

    def placeorder(self, **kwargs):
        self.calls.append(("order", kwargs))
        return {"status": "success", "orderid": "1"}

    def cancelorder(self, **kwargs):
        self.calls.append(("cancel", kwargs))
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


def test_raw_sdk_client_is_refused_for_orders_and_cancels():
    sdk = _Sdk()
    assert not has_engine_order_method(sdk) and not has_engine_cancel_method(sdk)
    with pytest.raises(PermissionError):
        send_engine_order(sdk, symbol="SBIN", quantity=1)
    with pytest.raises(PermissionError):
        send_engine_cancel(sdk, order_id="1")
    assert sdk.calls == []


def test_paper_broker_gets_orders_and_cancels():
    paper = _Paper()
    assert has_engine_order_method(paper) and has_engine_cancel_method(paper)
    assert send_engine_order(paper, symbol="SBIN", quantity=1)["status"] == "success"
    assert send_engine_cancel(paper, order_id="1")["status"] == "success"
    assert [c[0] for c in paper.calls] == ["order", "cancel"]


def test_gateway_sends_when_analyzer_is_on():
    sdk = _Sdk()
    gw = _gateway(sdk, analyze_on=True)
    assert send_engine_order(gw, symbol="SBIN", quantity=1)["status"] == "success"
    assert send_engine_cancel(gw, order_id="1")["status"] == "success"
    assert [c[0] for c in sdk.calls] == ["order", "cancel"]


def test_gateway_refuses_when_analyzer_is_off_and_never_calls_sdk():
    sdk = _Sdk()
    gw = _gateway(sdk, analyze_on=False)
    with pytest.raises(PermissionError):
        send_engine_order(gw, symbol="SBIN", quantity=1)
    with pytest.raises(PermissionError):
        send_engine_cancel(gw, order_id="1")
    assert sdk.calls == []


def test_gateway_refuses_empty_api_key():
    sdk = _Sdk()
    gw = _gateway(sdk, analyze_on=True, api_key="")
    with pytest.raises(PermissionError):
        send_engine_order(gw, symbol="SBIN", quantity=1)
    assert sdk.calls == []


def test_gateway_live_mode_needs_approval():
    sdk = _Sdk()
    guard = OpenAlgoClient("http://x", _analyzer(True), mode="live")  # no LiveApproval
    gw = EngineOrderGateway(sdk, guard, "key")
    with pytest.raises(PermissionError):
        send_engine_order(gw, symbol="SBIN", quantity=1)
    assert sdk.calls == []


def test_gateway_requires_an_order_client_guard():
    with pytest.raises(TypeError):
        EngineOrderGateway(_Sdk(), object(), "key")
