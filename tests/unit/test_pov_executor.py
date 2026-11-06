"""POV child executor: gate -> queue -> one client, with fakes and the real gate (decision 0021).

Flow stage: Algo router, Risk gate, Approved order queue, OpenAlgo
Status: TEST
"""
import pytest

from quant.core.config import RiskLimits
from quant.core.types import GateDecision, TradeIntent
from quant.execution.pov_executor import PovExecutor
from quant.monitor.safe_mode import Mode, SafeMode
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch


class FakeSender:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        self.calls.append((intent, api_key, decision, price_type, limit_price))
        if self.fail is not None:
            raise self.fail
        return {"status": "ok", "orderid": f"O{len(self.calls)}"}


class FakeAudit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))
        return "hash"

    def kinds(self):
        return [k for k, _ in self.rows]


PARENT = TradeIntent(intent_id="P1", strategy_id="S1", symbol="INFY", side="BUY", qty=500,
                     ref_price=1000.0, kind="entry", lot_size=1, prev_close=1000.0,
                     circuit_pct=20.0, adv_shares=1_000_000.0)

LIMITS = RiskLimits(max_order_quantity=1000, max_order_notional=10_000_000.0,
                    max_open_exposure=1e9, max_daily_loss=1e6, max_strategy_loss=1e6,
                    max_adv_pct=0.5)


def real_gate(tmp_path, safe=None, kill=None):
    state = RiskState()
    kill = kill or KillSwitch(tmp_path / "kill.json")
    safe = safe or SafeMode()
    return lambda intent: check_intent(intent, state, LIMITS, kill, safe)


def make(gate, sender=None, audit=None, key="key", pov=0.1):
    sender = sender or FakeSender()
    audit = audit or FakeAudit()
    return PovExecutor(gate, sender, key, audit, pov=pov), sender, audit


def test_approved_child_reaches_client_once(tmp_path):
    ex, sender, audit = make(real_gate(tmp_path))
    child = PovExecutor.make_child(PARENT, qty=50, seq=1)
    out = ex.submit_child(child)
    assert out.sent and out.reasons == () and out.response["status"] == "ok"
    assert len(sender.calls) == 1
    intent, key, decision, price_type, limit_price = sender.calls[0]
    assert intent.intent_id == "P1-C0001" and key == "key"
    assert decision.approved and decision.intent_id == "P1-C0001"
    assert price_type == "MARKET" and limit_price is None
    assert audit.kinds() == ["gate", "child_sent"]


def test_limit_price_passes_through(tmp_path):
    ex, sender, _ = make(real_gate(tmp_path))
    ex.submit_child(PovExecutor.make_child(PARENT, qty=50, seq=1), price_type="LIMIT", limit_price=1001.0)
    _, _, _, price_type, limit_price = sender.calls[0]
    assert price_type == "LIMIT" and limit_price == 1001.0


def test_kill_switch_stops_entry_child(tmp_path):
    kill = KillSwitch(tmp_path / "kill.json")
    kill.trip("test halt")
    ex, sender, audit = make(real_gate(tmp_path, kill=kill))
    out = ex.submit_child(PovExecutor.make_child(PARENT, qty=50, seq=1))
    assert not out.sent and sender.calls == []
    assert any("kill switch" in r for r in out.reasons)
    assert audit.kinds() == ["gate"]


def test_halt_all_safe_mode_stops_child(tmp_path):
    safe = SafeMode()
    safe.set(Mode.HALT_ALL, "test")
    ex, sender, _ = make(real_gate(tmp_path, safe=safe))
    out = ex.submit_child(PovExecutor.make_child(PARENT, qty=50, seq=1))
    assert not out.sent and sender.calls == []
    assert "safe mode: halt-all" in out.reasons


def test_child_over_order_quantity_limit_is_refused(tmp_path):
    ex, sender, _ = make(real_gate(tmp_path))
    out = ex.submit_child(PovExecutor.make_child(PARENT, qty=5000, seq=1))
    assert not out.sent and sender.calls == []


def test_duplicate_child_id_refused_without_second_gate_call(tmp_path):
    calls = []
    gate = real_gate(tmp_path)

    def counting_gate(intent):
        calls.append(intent.intent_id)
        return gate(intent)

    ex, sender, audit = make(counting_gate)
    child = PovExecutor.make_child(PARENT, qty=50, seq=1)
    assert ex.submit_child(child).sent
    second = ex.submit_child(child)
    assert not second.sent and "duplicate" in second.reasons[0]
    assert len(sender.calls) == 1 and calls == ["P1-C0001"]
    assert audit.kinds()[-1] == "child_refused"


def test_client_refusal_propagates_and_is_audited(tmp_path):
    ex, _, audit = make(real_gate(tmp_path), sender=FakeSender(fail=PermissionError("live guard")))
    with pytest.raises(PermissionError, match="live guard"):
        ex.submit_child(PovExecutor.make_child(PARENT, qty=50, seq=1))
    assert audit.kinds() == ["gate", "child_refused"]


def test_queue_refuses_decision_for_other_intent(tmp_path):
    """A gate that approves a different id must not get an order through."""
    sender = FakeSender()
    ex, _, _ = make(lambda intent: GateDecision("OTHER", True, ()), sender=sender)
    with pytest.raises(PermissionError, match="does not match"):
        ex.submit_child(PovExecutor.make_child(PARENT, qty=50, seq=1))
    assert sender.calls == []


def test_rejected_gate_decision_never_sent():
    sender = FakeSender()
    ex, _, _ = make(lambda intent: GateDecision(intent.intent_id, False, ("blocked",)), sender=sender)
    out = ex.submit_child(PovExecutor.make_child(PARENT, qty=50, seq=1))
    assert not out.sent and out.reasons == ("blocked",) and sender.calls == []


def test_child_qty_follows_pov_share_and_remaining():
    ex, _, _ = make(lambda i: GateDecision(i.intent_id, True, ()), pov=0.1)
    assert ex.child_qty(remaining=1000, bar_volume=2000) == 200
    assert ex.child_qty(remaining=50, bar_volume=2000) == 50
    assert ex.child_qty(remaining=1000, bar_volume=0) == 0


def test_make_child_copies_parent_and_numbers_ids():
    c = PovExecutor.make_child(PARENT, qty=7, seq=12)
    assert c.intent_id == "P1-C0012" and c.qty == 7
    assert (c.symbol, c.side, c.strategy_id, c.kind, c.ref_price) == ("INFY", "BUY", "S1", "entry", 1000.0)
    assert c.adv_shares == PARENT.adv_shares and c.circuit_pct == 20.0


@pytest.mark.parametrize("qty, seq", [(0, 1), (-1, 1), (5, 0)])
def test_make_child_rejects_bad_numbers(qty, seq):
    with pytest.raises(ValueError):
        PovExecutor.make_child(PARENT, qty=qty, seq=seq)


def test_empty_key_and_bad_pov_rejected(tmp_path):
    gate = real_gate(tmp_path)
    with pytest.raises(ValueError, match="API key"):
        make(gate, key="")
    for bad in (0, -0.1, 1.5):
        with pytest.raises(ValueError, match="pov"):
            make(gate, pov=bad)
