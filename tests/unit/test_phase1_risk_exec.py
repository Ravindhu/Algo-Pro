"""Phase 1: Risk gate, kill switch, safe mode, audit, queue, order state, broker adapter, ledger."""
import pytest

from quant.audit.log import AuditLog
from quant.core.config import RiskLimits
from quant.core.types import GateDecision, TradeIntent
from quant.execution.openalgo_client import OpenAlgoClient, PaperBroker
from quant.execution.order_fsm import InvalidTransition, OrderFSM, OrderState
from quant.execution.queue import ApprovedOrderQueue
from quant.monitor.ledger import Ledger
from quant.monitor.safe_mode import Mode, SafeMode
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch

FULL = RiskLimits(
    max_order_quantity=1000, max_order_notional=5e6, max_open_exposure=2e7,
    max_daily_loss=1e5, max_strategy_loss=5e4, max_adv_pct=0.05,
)


def intent(**kw):
    base = dict(intent_id="I1", strategy_id="S1", symbol="RELIANCE", side="BUY", qty=10,
                ref_price=1000.0, kind="entry", lot_size=1, prev_close=990.0,
                circuit_pct=0.2, adv_shares=1e6, asm_gsm=False)
    base.update(kw)
    return TradeIntent(**base)


@pytest.fixture
def kill(tmp_path):
    return KillSwitch(tmp_path / "kill.json")


@pytest.fixture
def safe():
    return SafeMode()


def test_unset_limits_fail_closed(kill, safe):
    d = check_intent(intent(), RiskState(), RiskLimits(), kill, safe)
    assert not d.approved
    assert any("limit not set" in r for r in d.reasons)


def test_clean_intent_passes_with_limits(kill, safe):
    d = check_intent(intent(), RiskState(), FULL, kill, safe)
    assert d.approved and d.reasons == ()


def test_kill_switch_blocks_entry_not_exit(kill, safe):
    kill.trip("test")
    assert not check_intent(intent(), RiskState(), FULL, kill, safe).approved
    assert check_intent(intent(kind="exit", side="SELL"), RiskState(), FULL, kill, safe).approved


def test_halt_all_blocks_everything_reduce_only_blocks_entries(kill):
    s = SafeMode()
    s.set(Mode.REDUCE_ONLY, "test")
    assert not check_intent(intent(), RiskState(), FULL, kill, s).approved
    assert check_intent(intent(kind="exit", side="SELL"), RiskState(), FULL, kill, s).approved
    s.set(Mode.HALT_ALL, "test")
    assert not check_intent(intent(kind="exit", side="SELL"), RiskState(), FULL, kill, s).approved


def test_duplicate_intent_blocked(kill, safe):
    st = RiskState(seen_ids={"I1"})
    assert not check_intent(intent(), st, FULL, kill, safe).approved


def test_price_band_and_asm_block(kill, safe):
    assert not check_intent(intent(ref_price=1300.0), RiskState(), FULL, kill, safe).approved
    assert not check_intent(intent(asm_gsm=True), RiskState(), FULL, kill, safe).approved


def test_liquidity_cap_and_unknown_adv(kill, safe):
    assert not check_intent(intent(qty=100_000), RiskState(), FULL, kill, safe).approved
    assert not check_intent(intent(adv_shares=None), RiskState(), FULL, kill, safe).approved


def test_loss_budget_blocks_entries(kill, safe):
    assert not check_intent(intent(), RiskState(daily_pnl=-1e5), FULL, kill, safe).approved
    st = RiskState(strategy_pnl={"S1": -6e4})
    assert not check_intent(intent(), st, FULL, kill, safe).approved


def test_exposure_limit(kill, safe):
    st = RiskState(open_notional=2e7 - 1000)
    assert not check_intent(intent(), st, FULL, kill, safe).approved


def test_kill_reset_needs_actor_and_reason(tmp_path):
    audit = AuditLog(tmp_path / "a.jsonl")
    k = KillSwitch(tmp_path / "k.json")
    k.trip("x")
    with pytest.raises(ValueError):
        k.reset("", "")
    k.reset("ravi", "checked feed", audit)
    assert not k.active
    assert audit.read_all()[-1]["kind"] == "override"


def test_audit_chain_detects_tampering(tmp_path):
    a = AuditLog(tmp_path / "a.jsonl")
    a.append("x", {"v": 1})
    a.append("y", {"v": 2})
    assert a.verify()
    text = (tmp_path / "a.jsonl").read_text().replace('"v": 1', '"v": 9')
    (tmp_path / "a.jsonl").write_text(text)
    assert not AuditLog(tmp_path / "a.jsonl").verify()


def test_queue_refuses_unapproved():
    q = ApprovedOrderQueue()
    i = intent()
    with pytest.raises(PermissionError):
        q.push(i, GateDecision("I1", False, ("x",)))
    with pytest.raises(PermissionError):
        q.push(i, GateDecision("OTHER", True))
    q.push(i, GateDecision("I1", True))
    assert len(q) == 1


def test_order_fsm_rejects_bad_transitions():
    f = OrderFSM("I1", 10)
    with pytest.raises(InvalidTransition):
        f.move(OrderState.FILLED, 10)
    f.move(OrderState.SUBMITTED)
    with pytest.raises(ValueError):
        f.move(OrderState.FILLED, 5)
    f.move(OrderState.FILLED, 10)
    with pytest.raises(InvalidTransition):
        f.move(OrderState.CANCELLED)


def test_openalgo_refuses_in_paper_and_without_approval():
    calls = []
    transport = lambda url, payload: calls.append((url, payload)) or {"status": "ok"}
    with pytest.raises(PermissionError):
        OpenAlgoClient("http://x", transport, mode="paper", live_approval="ok").place(intent(), "k")
    with pytest.raises(PermissionError):
        OpenAlgoClient("http://x", transport, mode="live", live_approval=None).place(intent(), "k")
    assert calls == []
    OpenAlgoClient("http://x", transport, mode="live", live_approval="ravi-2026-11-03").place(intent(), "k")
    assert calls and calls[0][0].endswith("/api/v1/placeorder")


def test_paper_broker_fills_with_slippage():
    fill = PaperBroker(slippage_bps=10).place(intent(side="BUY"))
    assert fill["status"] == "FILLED" and fill["avg_price"] > 1000.0


def test_ledger_positions_and_recon(tmp_path):
    L = Ledger(tmp_path / "l.jsonl")
    L.append("fill", intent_id="I1", symbol="A", signed_qty=10, price=1.0)
    L.append("fill", intent_id="I2", symbol="A", signed_qty=-4, price=1.0)
    assert L.positions() == {"A": 6}
    assert L.recon({"A": 5}) == {"A": (6, 5)}
    assert L.recon({"A": 6}) == {}
