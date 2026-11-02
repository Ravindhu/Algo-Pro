

# --------------------------------------------------------------------------- #
# require_live_ack (audit 2026-10-29): a live run must state its money caps.
# --------------------------------------------------------------------------- #
def _ack_gate(**risk):
    from blue_engine.core.risk_gate import RiskGate
    return RiskGate({"risk": {"require_live_ack": True, **risk}})


def test_live_ack_rejects_a_live_intent_with_no_monetary_limit():
    decision = _ack_gate().check({"action": "BUY", "quantity": 10}, live=True)
    assert decision.approved is False
    assert "require_live_ack" in decision.reason
    # Every missing cap is named, so the operator knows what to configure.
    for name in ("max_order_notional", "max_daily_loss", "max_open_exposure"):
        assert name in decision.reason


def test_live_ack_does_not_apply_outside_live():
    # analyze/paper runs keep their previous behaviour.
    assert _ack_gate().check({"action": "BUY", "quantity": 10}, live=False).approved


def test_live_ack_is_required_by_default():
    from blue_engine.core.risk_gate import RiskGate
    # Audit 2026-11-02: an unconfigured live gate now fails closed.
    assert not RiskGate({}).check({"action": "BUY", "quantity": 10}, live=True).approved


def test_live_ack_can_be_disabled_explicitly():
    from blue_engine.core.risk_gate import RiskGate
    # Explicit opt-out keeps the old permissive behaviour for analyze-style runs.
    assert RiskGate({"require_live_ack": False}).check(
        {"action": "BUY", "quantity": 10}, live=True).approved


def test_order_quantity_alone_does_not_satisfy_the_ack():
    # A share count is not a rupee cap: 100 shares is tiny or huge depending
    # on price, so it cannot acknowledge monetary risk on its own.
    decision = _ack_gate(max_order_quantity=500).check(
        {"action": "BUY", "quantity": 10}, live=True)
    assert decision.approved is False
    assert "max_order_notional" in decision.reason


def test_live_ack_passes_when_every_money_cap_is_configured():
    gate = _ack_gate(max_order_notional=250000.0, max_daily_loss=10000.0,
                     max_open_exposure=500000.0)
    decision = gate.check({"action": "BUY", "quantity": 10, "price": 100.0},
                          day_pnl=0.0, open_exposure=0.0, live=True)
    assert decision.approved is True


def test_live_ack_still_rejects_a_partial_profile():
    gate = _ack_gate(max_order_notional=250000.0, max_daily_loss=10000.0)
    decision = gate.check({"action": "BUY", "quantity": 10, "price": 100.0},
                          day_pnl=0.0, open_exposure=0.0, live=True)
    assert decision.approved is False
    assert "max_open_exposure" in decision.reason


# --------------------------------------------------------------------------- #
# Reduce-only exits (audit 2026-11-03, fix 1): a stop-loss must not be trapped
# by a stale P&L feed, an unknown price, the kill switch, or a size cap.
# --------------------------------------------------------------------------- #
_CAPPED = dict(max_order_quantity=100, max_order_notional=50000.0,
               max_daily_loss=10000.0, max_open_exposure=200000.0)


def test_exit_is_allowed_when_daily_pnl_is_unverifiable():
    gate = _ack_gate(**_CAPPED)
    decision = gate.check({"action": "SELL", "quantity": 50, "price": 100.0},
                          day_pnl=None, open_exposure=None, reduces_exposure=True,
                          position_quantity=50, live=True)
    assert decision.approved is True, decision.reason


def test_exit_is_allowed_after_the_daily_loss_cap_is_hit():
    gate = _ack_gate(**_CAPPED)
    decision = gate.check({"action": "SELL", "quantity": 50, "price": 100.0},
                          day_pnl=-20000.0, open_exposure=150000.0,
                          reduces_exposure=True, position_quantity=50, live=True)
    assert decision.approved is True, decision.reason


def test_exit_is_allowed_when_the_gate_is_blocked():
    gate = _ack_gate(blocked=True, **_CAPPED)
    decision = gate.check({"action": "SELL", "quantity": 50, "price": 100.0},
                          day_pnl=0.0, open_exposure=0.0,
                          reduces_exposure=True, position_quantity=50, live=True)
    assert decision.approved is True, decision.reason


def test_exit_larger_than_the_position_is_rejected_as_a_flip():
    # The excess would open a fresh short, so it must pass full checks.
    gate = _ack_gate(**_CAPPED)
    decision = gate.check({"action": "SELL", "quantity": 80, "price": 100.0},
                          day_pnl=0.0, open_exposure=0.0,
                          reduces_exposure=True, position_quantity=50, live=True)
    assert decision.approved is False
    assert "exceeds open position" in decision.reason


def test_entry_still_rejected_when_daily_pnl_is_unverifiable():
    gate = _ack_gate(**_CAPPED)
    decision = gate.check({"action": "BUY", "quantity": 10, "price": 100.0},
                          day_pnl=None, open_exposure=0.0, live=True)
    assert decision.approved is False
    assert "max_daily_loss" in decision.reason


def test_entry_still_rejected_when_the_gate_is_blocked():
    gate = _ack_gate(blocked=True, **_CAPPED)
    decision = gate.check({"action": "BUY", "quantity": 10, "price": 100.0},
                          day_pnl=0.0, open_exposure=0.0, live=True)
    assert decision.approved is False
    assert "blocked" in decision.reason


def test_unsized_exit_keeps_the_legacy_caps_but_not_unknown_inputs():
    # No position_quantity: the gate cannot prove reduce-only, so the size cap
    # still applies, but a missing quote must not block the exit.
    gate = _ack_gate(**_CAPPED)
    oversized = gate.check({"action": "SELL", "quantity": 500, "price": 100.0},
                           day_pnl=None, open_exposure=None, reduces_exposure=True)
    assert oversized.approved is False
    assert "max_order_quantity" in oversized.reason
    unpriced = gate.check({"action": "SELL", "quantity": 50},
                          day_pnl=None, open_exposure=None, reduces_exposure=True)
    assert unpriced.approved is True, unpriced.reason


# --------------------------------------------------------------------------- #
# Halt policy (audit 2026-11-03): a kill-switch halt stops new risk only.
# --------------------------------------------------------------------------- #
def test_halt_blocks_entries_but_not_exits():
    from blue_engine.core.risk_gate import halt_blocks_order
    assert halt_blocks_order(True, False) is True    # entry while halted
    assert halt_blocks_order(True, True) is False    # exit while halted
    assert halt_blocks_order(False, False) is False  # entry, no halt
    assert halt_blocks_order(False, True) is False   # exit, no halt


def test_unknown_intent_under_halt_is_treated_as_an_entry():
    from blue_engine.core.risk_gate import halt_blocks_order
    # reduces_exposure defaults to False, so an unclassified order is blocked.
    assert halt_blocks_order(True, None) is True
