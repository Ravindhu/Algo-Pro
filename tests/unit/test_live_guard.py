"""Live order guard: every refusal path and the one allowed path (decision 0017).

Flow stage: OpenAlgo
Status: PHASE 1 IMPLEMENTED
"""
from datetime import date

import pytest

from quant.core.types import GateDecision, TradeIntent
from quant.execution.openalgo_client import LiveApproval, OpenAlgoClient, file_sha256
from quant.risk.compliance import ComplianceResult


def intent(intent_id="I1"):
    return TradeIntent(intent_id=intent_id, strategy_id="S1", symbol="INFY", side="BUY", qty=10,
                       ref_price=1500.0, kind="entry", lot_size=1)


@pytest.fixture
def limits(tmp_path):
    path = tmp_path / "risk_limits.yaml"
    path.write_text("max_order_quantity: 10\n", encoding="utf-8")
    return path


@pytest.fixture
def approval(limits):
    return LiveApproval("owner", date(2026, 11, 6), file_sha256(limits))


@pytest.fixture
def calls():
    return []


@pytest.fixture
def transport(calls):
    return lambda url, payload: calls.append((url, payload)) or {"status": "ok"}


def client(transport, limits, approval, mode="live"):
    # Passing compliance gates: this suite tests the order guards, not compliance
    # (quant/tests/test_compliance.py covers the gate itself).
    ok = ComplianceResult(True, ())
    gate = type("OkGate", (), {"check": staticmethod(lambda *a, **k: ok)})()
    return OpenAlgoClient("http://x", transport, mode=mode, live_approval=approval,
                          limits_path=limits, compliance=gate)


APPROVED = GateDecision("I1", True, ())


def test_happy_path_sends_one_order(transport, limits, approval, calls):
    client(transport, limits, approval).place(intent(), "key", APPROVED)
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "http://x/api/v1/placeorder"
    assert payload["symbol"] == "INFY" and payload["quantity"] == 10 and payload["tag"] == "I1"


def test_paper_mode_refused_even_with_approval(transport, limits, approval, calls):
    with pytest.raises(PermissionError, match="mode is live"):
        client(transport, limits, approval, mode="paper").place(intent(), "key", APPROVED)
    assert calls == []


def test_plain_string_approval_refused(transport, limits, calls):
    ok = ComplianceResult(True, ())
    gate = type("OkGate", (), {"check": staticmethod(lambda *a, **k: ok)})()
    c = OpenAlgoClient("http://x", transport, mode="live", live_approval="ravi-2026-11-03",
                       limits_path=limits, compliance=gate)
    with pytest.raises(PermissionError, match="LiveApproval"):
        c.place(intent(), "key", APPROVED)
    assert calls == []


def test_missing_approval_refused(transport, limits, calls):
    with pytest.raises(PermissionError, match="LiveApproval"):
        client(transport, limits, None).place(intent(), "key", APPROVED)
    assert calls == []


def test_blank_approver_refused(transport, limits, calls):
    blank = LiveApproval("   ", date(2026, 11, 6), file_sha256(limits))
    with pytest.raises(PermissionError, match="LiveApproval"):
        client(transport, limits, blank).place(intent(), "key", APPROVED)
    assert calls == []


def test_missing_limits_path_refused(transport, approval, calls):
    ok = ComplianceResult(True, ())
    gate = type("OkGate", (), {"check": staticmethod(lambda *a, **k: ok)})()
    c = OpenAlgoClient("http://x", transport, mode="live", live_approval=approval,
                       limits_path=None, compliance=gate)
    with pytest.raises(PermissionError, match="limits file path"):
        c.place(intent(), "key", APPROVED)
    assert calls == []


def test_changed_limits_void_the_approval(transport, limits, approval, calls):
    limits.write_text("max_order_quantity: 999\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="approval is void"):
        client(transport, limits, approval).place(intent(), "key", APPROVED)
    assert calls == []


def test_missing_decision_refused(transport, limits, approval, calls):
    with pytest.raises(PermissionError, match="no approved gate decision"):
        client(transport, limits, approval).place(intent(), "key", None)
    assert calls == []


def test_rejected_decision_refused(transport, limits, approval, calls):
    rejected = GateDecision("I1", False, ("limit not set: max_daily_loss",))
    with pytest.raises(PermissionError, match="no approved gate decision"):
        client(transport, limits, approval).place(intent(), "key", rejected)
    assert calls == []


def test_decision_for_other_intent_refused(transport, limits, approval, calls):
    other = GateDecision("OTHER", True, ())
    with pytest.raises(PermissionError, match="does not match"):
        client(transport, limits, approval).place(intent(), "key", other)
    assert calls == []


def test_string_decision_refused(transport, limits, approval, calls):
    with pytest.raises(PermissionError, match="no approved gate decision"):
        client(transport, limits, approval).place(intent(), "key", "approved")
    assert calls == []


def test_empty_api_key_refused(transport, limits, approval, calls):
    with pytest.raises(PermissionError, match="empty API key"):
        client(transport, limits, approval).place(intent(), "", APPROVED)
    assert calls == []


# --- place_child (decision 0021): same guard, MARKET or LIMIT ---

def test_child_market_sends_one_order(transport, limits, approval, calls):
    client(transport, limits, approval).place_child(intent(), "key", APPROVED)
    url, payload = calls[0]
    assert url == "http://x/api/v1/placeorder"
    assert payload["pricetype"] == "MARKET" and "price" not in payload


def test_child_limit_sends_price(transport, limits, approval, calls):
    client(transport, limits, approval).place_child(intent(), "key", APPROVED, "LIMIT", 1501.5)
    _, payload = calls[0]
    assert payload["pricetype"] == "LIMIT" and payload["price"] == 1501.5


@pytest.mark.parametrize("bad", [None, 0, -1.0, float("nan"), float("inf")])
def test_child_limit_needs_valid_price(transport, limits, approval, calls, bad):
    with pytest.raises(ValueError, match="limit_price"):
        client(transport, limits, approval).place_child(intent(), "key", APPROVED, "LIMIT", bad)
    assert calls == []


@pytest.mark.parametrize("bad_type", ["PEG", "SL", "", "market"])
def test_child_unknown_price_type_refused(transport, limits, approval, calls, bad_type):
    with pytest.raises(ValueError, match="unsupported price_type"):
        client(transport, limits, approval).place_child(intent(), "key", APPROVED, bad_type)
    assert calls == []


def test_child_paper_mode_refused(transport, limits, approval, calls):
    with pytest.raises(PermissionError, match="mode is live"):
        client(transport, limits, approval, mode="paper").place_child(intent(), "key", APPROVED)
    assert calls == []


def test_child_rejected_decision_refused(transport, limits, approval, calls):
    rejected = GateDecision("I1", False, ("breach",))
    with pytest.raises(PermissionError, match="no approved gate decision"):
        client(transport, limits, approval).place_child(intent(), "key", rejected)
    assert calls == []


def test_child_changed_limits_refused(transport, limits, approval, calls):
    limits.write_text("max_order_quantity: 999\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="approval is void"):
        client(transport, limits, approval).place_child(intent(), "key", APPROVED)
    assert calls == []


def test_child_empty_key_refused(transport, limits, approval, calls):
    with pytest.raises(PermissionError, match="empty API key"):
        client(transport, limits, approval).place_child(intent(), "", APPROVED)
    assert calls == []


# --- cancel (decision 0021): session guard only, no GateDecision ---

def test_cancel_happy_path_sends_cancel(transport, limits, approval, calls):
    client(transport, limits, approval).cancel("2609001", "S1", "key")
    url, payload = calls[0]
    assert url == "http://x/api/v1/cancelorder"
    assert payload == {"apikey": "key", "orderid": "2609001", "strategy": "S1"}


def test_cancel_paper_mode_refused(transport, limits, approval, calls):
    with pytest.raises(PermissionError, match="mode is live"):
        client(transport, limits, approval, mode="paper").cancel("1", "S1", "key")
    assert calls == []


def test_cancel_without_approval_refused(transport, limits, calls):
    with pytest.raises(PermissionError, match="LiveApproval"):
        client(transport, limits, None).cancel("1", "S1", "key")
    assert calls == []


def test_cancel_changed_limits_refused(transport, limits, approval, calls):
    limits.write_text("max_order_quantity: 999\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="approval is void"):
        client(transport, limits, approval).cancel("1", "S1", "key")
    assert calls == []


def test_cancel_empty_key_refused(transport, limits, approval, calls):
    with pytest.raises(PermissionError, match="empty API key"):
        client(transport, limits, approval).cancel("1", "S1", "")
    assert calls == []


def test_cancel_needs_order_id(transport, limits, approval, calls):
    with pytest.raises(ValueError, match="order_id"):
        client(transport, limits, approval).cancel("", "S1", "key")
    assert calls == []
