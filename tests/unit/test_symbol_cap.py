"""Max symbols per portfolio: Risk gate check and open-symbol set from the ledger (owner request 2026-11-07)."""
import sys
from pathlib import Path

import pytest

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.monitor.ledger import Ledger
from quant.monitor.safe_mode import SafeMode
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
from quant.risk.symbol_cap import symbol_cap_checks

ROOT = Path(__file__).resolve().parents[2]


def limits(cap):
    return RiskLimits(max_order_quantity=1000, max_order_notional=5e6, max_open_exposure=2e7,
                      max_daily_loss=1e5, max_strategy_loss=5e4, max_adv_pct=0.05,
                      max_symbols_per_portfolio=cap)


def intent(symbol="RELIANCE", kind="entry", side="BUY"):
    return TradeIntent(intent_id=f"I-{symbol}-{kind}", strategy_id="S1", symbol=symbol, side=side, qty=10,
                       ref_price=1000.0, kind=kind, lot_size=1, prev_close=990.0,
                       circuit_pct=0.2, adv_shares=1e6, asm_gsm=False)


@pytest.fixture
def kill(tmp_path):
    return KillSwitch(tmp_path / "kill.json")


def test_unset_cap_blocks_new_symbols():
    assert symbol_cap_checks(intent(), set(), limits(None)) == ["limit not set: max_symbols_per_portfolio"]


def test_new_symbol_blocked_at_cap():
    reasons = symbol_cap_checks(intent("RELIANCE"), {"TCS", "INFY"}, limits(2))
    assert reasons == ["max symbols per portfolio reached: 2/2"]


def test_held_symbol_allowed_at_cap():
    assert symbol_cap_checks(intent("TCS"), {"TCS", "INFY"}, limits(2)) == []


def test_new_symbol_allowed_under_cap():
    assert symbol_cap_checks(intent("RELIANCE"), {"TCS"}, limits(2)) == []


def test_gate_blocks_new_symbol_at_cap(kill):
    state = RiskState(open_symbols={"TCS", "INFY"})
    decision = check_intent(intent("RELIANCE"), state, limits(2), kill, SafeMode())
    assert not decision.approved
    assert any("max symbols per portfolio reached" in r for r in decision.reasons)


def test_gate_approves_held_symbol_at_cap(kill):
    state = RiskState(open_symbols={"TCS", "INFY"})
    assert check_intent(intent("TCS"), state, limits(2), kill, SafeMode()).approved


def test_gate_does_not_block_exits_at_cap(kill):
    state = RiskState(open_symbols={"TCS", "INFY"})
    exit_intent = intent("RELIANCE", kind="exit", side="SELL")
    assert check_intent(exit_intent, state, limits(2), kill, SafeMode()).approved


def test_open_symbols_come_from_net_filled_quantity(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    from run_paper import _open_symbols

    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append("fill", intent_id="a", symbol="TCS", signed_qty=10, price=1.0)
    ledger.append("fill", intent_id="b", symbol="INFY", signed_qty=5, price=1.0)
    ledger.append("fill", intent_id="c", symbol="INFY", signed_qty=-5, price=1.0)  # closed out
    assert _open_symbols(ledger) == {"TCS"}
