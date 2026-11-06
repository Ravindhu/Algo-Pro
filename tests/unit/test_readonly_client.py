"""Read-only OpenAlgo adapter: reads go to read paths only, and no order method exists (decision 0021).

Flow stage: OpenAlgo
Status: TEST
"""
import pytest

from quant.execution.readonly_client import OpenAlgoReadOnly


@pytest.fixture
def calls():
    return []


@pytest.fixture
def ro(calls):
    return OpenAlgoReadOnly("http://x/", lambda url, payload: calls.append((url, payload)) or {"ok": True})


def test_quotes_depth_orderbook_hit_read_paths_only(ro, calls):
    ro.quotes("key", "INFY", "NSE")
    ro.depth("key", "INFY", "NSE")
    ro.orderbook("key")
    urls = [u for u, _ in calls]
    assert urls == [
        "http://x/api/v1/quotes",
        "http://x/api/v1/depth",
        "http://x/api/v1/orderbook",
    ]
    assert calls[0][1] == {"apikey": "key", "symbol": "INFY", "exchange": "NSE"}
    assert calls[2][1] == {"apikey": "key"}


def test_returns_transport_answer(ro):
    assert ro.quotes("key", "INFY", "NSE") == {"ok": True}


@pytest.mark.parametrize("call", [
    lambda c: c.quotes("", "INFY", "NSE"),
    lambda c: c.depth("", "INFY", "NSE"),
    lambda c: c.orderbook(""),
])
def test_empty_key_refused_without_sending(ro, calls, call):
    with pytest.raises(ValueError, match="empty API key"):
        call(ro)
    assert calls == []


def test_no_order_capable_method_exists():
    """Reads cannot place or cancel: no attribute name carries an order verb."""
    names = [n for n in dir(OpenAlgoReadOnly) if not n.startswith("_") and n.islower()]
    assert sorted(names) == ["depth", "orderbook", "quotes"]
    for name in names:
        assert not any(verb in name for verb in ("place", "cancel", "modify", "order_", "close", "exit"))


def test_module_names_no_order_route():
    import inspect
    import quant.execution.readonly_client as mod

    src = inspect.getsource(mod).lower()
    assert "placeorder" not in src and "cancelorder" not in src and "modifyorder" not in src
