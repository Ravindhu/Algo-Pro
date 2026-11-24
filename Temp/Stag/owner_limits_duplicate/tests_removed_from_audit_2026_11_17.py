# Moved out of src/quant/tests/test_audit_2026_11_17_fixes.py on 2026-11-25 with the duplicate owner_limits.
# They tested the duplicate's apply_owner_limits/unset_owner_caps. Live equivalent: core/owner_limits_store.

def test_a_null_owner_cap_is_reported_as_unset():
    owner = {"max_order_quantity": 10, "max_sector_pct": None}
    unset = unset_owner_caps(owner)
    assert "max_sector_pct" in unset
    assert "max_order_quantity" not in unset
    assert "max_beta" in unset


def test_a_null_cap_does_not_switch_the_gate_check_off():
    """Null adds nothing to the config, so the gate's own fail-closed policy
    applies: a live intent with no monetary cap is refused."""
    owner = {"max_order_quantity": 10, "max_order_notional": None,
             "max_daily_loss": None, "max_open_exposure": None}
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "risk_limits.yaml"
        path.write_text(
            "max_order_quantity: 10\nmax_order_notional: null\n"
            "max_daily_loss: null\nmax_open_exposure: null\n", encoding="utf-8")
        risk = apply_owner_limits({"risk": {}}, path)["risk"]
    assert "max_order_notional" not in risk

    from quant.engines.core.risk_gate import RiskGate
    decision = RiskGate(risk).check({"action": "BUY", "quantity": 1,
                                     "price": 100.0}, live=True)
    assert decision.approved is False
    assert any("require_live_ack" in b for b in decision.breaches)


# --------------------------------------------------------------------------- #
# Item 6: slippage units are declared, not guessed.
# --------------------------------------------------------------------------- #