

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


def test_live_ack_is_opt_in():
    from blue_engine.core.risk_gate import RiskGate
    # Without the flag, an unconfigured gate stays permissive (backward compat).
    assert RiskGate({}).check({"action": "BUY", "quantity": 10}, live=True).approved


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
