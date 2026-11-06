"""Live order guard: every refusal path and the one allowed path (decision 0017).

Flow stage: OpenAlgo
Status: PHASE 1 IMPLEMENTED
"""
from datetime import date

import pytest

from quant.core.types import GateDecision, TradeIntent
from quant.execution.openalgo_client import LiveApproval, OpenAlgoClient, file_sha256


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
    return OpenAlgoClient("http://x", transport, mode=mode, live_approval=approval, limits_path=limits)


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
    c = OpenAlgoClient("http://x", transport, mode="live", live_approval="ravi-2026-11-03", limits_path=limits)
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
    c = OpenAlgoClient("http://x", transport, mode="live", live_approval=approval, limits_path=None)
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
