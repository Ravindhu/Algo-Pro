"""Live orders follow OpenAlgo's analyzer switch (decision of 2026-11-25).

LiveApproval and the egress IP check were removed. Rules tested here:
  * analyzer ON (sandbox): no live order is sent, even with the bot set to live.
  * analyzer OFF: a live order is sent when the compliance gate passes.
  * analyzer status unclear: refused (fail closed).
  * analyze mode still needs the analyzer ON.
"""
import pytest

from quant.core.trading_mode import request_mode, trading_mode_status
from quant.core.types import GateDecision, TradeIntent
from quant.execution.openalgo_client import OpenAlgoClient
from quant.risk.compliance import ComplianceResult


class _Gate:
    def __init__(self, ok=True):
        self.ok = ok

    def check(self):
        return ComplianceResult(ok=self.ok, reasons=() if self.ok else ("audit storage down",))


def _intent():
    return TradeIntent(intent_id="i1", strategy_id="s", symbol="INFY", side="BUY", qty=10,
                       ref_price=100.0)


APPROVED = GateDecision(intent_id="i1", approved=True, reasons=())


def _transport(analyze_mode, calls, status="success"):
    def send(url, payload):
        calls.append(url)
        if url.endswith("/api/v1/analyzer"):
            return {"status": status, "data": {"analyze_mode": analyze_mode}}
        return {"status": "success", "orderid": "1"}
    return send


def _sent_orders(calls):
    return [u for u in calls if u.endswith("/api/v1/placeorder")]


def test_live_order_refused_while_analyzer_is_on():
    calls = []
    client = OpenAlgoClient("http://x", _transport(True, calls), mode="live", compliance=_Gate())
    with pytest.raises(PermissionError, match="analyzer"):
        client.place(_intent(), "key", APPROVED)
    assert _sent_orders(calls) == []


def test_live_order_sent_when_analyzer_is_off():
    calls = []
    client = OpenAlgoClient("http://x", _transport(False, calls), mode="live", compliance=_Gate())
    client.place(_intent(), "key", APPROVED)
    assert len(_sent_orders(calls)) == 1


@pytest.mark.parametrize("bad", ["unclear", "not_success"])
def test_live_order_refused_when_analyzer_status_unclear(bad):
    calls = []
    if bad == "unclear":
        transport = lambda url, payload: ({"status": "success", "data": {}} if url.endswith("/analyzer")
                                          else {"status": "success"})
    else:
        transport = _transport(False, calls, status="error")
    client = OpenAlgoClient("http://x", transport, mode="live", compliance=_Gate())
    with pytest.raises(PermissionError):
        client.place(_intent(), "key", APPROVED)


def test_live_order_refused_when_compliance_fails():
    calls = []
    client = OpenAlgoClient("http://x", _transport(False, calls), mode="live", compliance=_Gate(False))
    with pytest.raises(PermissionError, match="compliance"):
        client.place(_intent(), "key", APPROVED)
    assert _sent_orders(calls) == []


def test_analyze_order_refused_when_analyzer_is_off():
    calls = []
    client = OpenAlgoClient("http://x", _transport(False, calls), mode="analyze")
    with pytest.raises(PermissionError, match="analyze"):
        client.place(_intent(), "key", APPROVED)


def test_trading_mode_live_needs_no_approval(tmp_path):
    state = tmp_path / "mode.json"
    ok, _ = request_mode("live", state_path=state)
    assert ok
    st = trading_mode_status(state_path=state)
    assert st["mode"] == "live" and st["effective_mode"] == "live"
