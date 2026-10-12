"""
tests/test_risk_overrides.py — risk controls override strategy signals
(AGENTS.md:498-520).

The risk-management policy says risk controls must be capable of OVERRIDING
strategy signals when predefined limits are breached. These tests prove that
for every configured limit: a perfectly valid, well-formed strategy intent
(BUY 100 @ 100) is still rejected when the limit is breached.

The gate under test is ``blue_engine.core.risk_gate.RiskGate`` — the
pre-trade check the runner runs on the hot path before any slicer. The
policy-level ``decide()`` kill-switch override is covered by
``tests/test_recovery.py`` and ``tests/test_optim_policy.py``.

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests/test_risk_overrides.py -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_risk_overrides.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.core.risk_gate import RiskGate, check_intent  # noqa: E402
from blue_engine.portfolio import optim_policy as op  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _signal(**over):
    """A textbook-valid strategy signal: BUY 100 shares at 100."""
    d = {"action": "BUY", "quantity": 100, "price": 100.0}
    d.update(over)
    return d


def _gate(**limits):
    limits.setdefault("allowed_actions", ["BUY", "SELL"])
    return RiskGate(limits)


def _assert_rejected(decision, which: str) -> None:
    _check(decision.approved is False,
           f"a {which} breach must override the valid signal: "
           f"{decision.reason}")
    _check(len(decision.breaches) > 0,
           f"the {which} rejection must name a breach: {decision.breaches}")


# --------------------------------------------------------------------------- #
# 1. Master kill-switch overrides everything
# --------------------------------------------------------------------------- #
def test_master_kill_switch_overrides_valid_signal():
    decision = _gate(blocked=True).check(_signal())
    _assert_rejected(decision, "kill-switch")


# --------------------------------------------------------------------------- #
# 2. Size limits override the signal
# --------------------------------------------------------------------------- #
def test_max_position_size_overrides_valid_signal():
    decision = _gate(max_order_quantity=50).check(_signal(quantity=100))
    _assert_rejected(decision, "max_order_quantity")


def test_max_order_notional_overrides_valid_signal():
    # 100 shares x 100 = 10,000 notional vs a 5,000 cap.
    decision = _gate(max_order_notional=5_000).check(_signal())
    _assert_rejected(decision, "max_order_notional")


def test_monetary_limit_rejects_missing_or_invalid_price():
    gate = _gate(max_order_notional=5_000)
    for price in (None, 0, -1, True, float("nan"), float("inf"), "invalid"):
        decision = gate.check(_signal(price=price), require_intent_price=True)
        _assert_rejected(decision, f"invalid price {price!r}")


def test_intent_price_does_not_fall_back_to_configured_reference():
    gate = _gate(max_order_notional=5_000, price=100)
    decision = gate.check(
        {"action": "BUY", "quantity": 100}, require_intent_price=True)
    _assert_rejected(decision, "missing live intent price")


def test_configured_daily_loss_limit_rejects_missing_pnl():
    decision = _gate(max_daily_loss=25_000).check(_signal())
    _assert_rejected(decision, "missing daily P&L")


def test_configured_exposure_limit_rejects_missing_exposure():
    decision = _gate(max_open_exposure=5_000_000).check(_signal())
    _assert_rejected(decision, "missing current exposure")


# --------------------------------------------------------------------------- #
# 3. Daily-loss cap overrides the signal
# --------------------------------------------------------------------------- #
def test_max_daily_loss_overrides_valid_signal():
    # day_pnl is signed: -30,000 loss vs a 25,000 cap.
    gate = _gate(max_daily_loss=25_000)
    decision = gate.check(_signal(), day_pnl=-30_000)
    _assert_rejected(decision, "max_daily_loss")


def test_max_daily_loss_exactly_at_cap_rejects():
    gate = _gate(max_daily_loss=25_000)
    decision = gate.check(_signal(), day_pnl=-25_000)
    _assert_rejected(decision, "max_daily_loss (at the cap)")


def test_small_daily_loss_does_not_block_valid_signal():
    """Inside the limit the same signal must PASS — the gate is a cap,
    not a blanket ban."""
    gate = _gate(max_daily_loss=25_000)
    decision = gate.check(_signal(), day_pnl=-1_000)
    _check(decision.approved is True,
           f"an in-limit signal must be approved: {decision.reason}")


# --------------------------------------------------------------------------- #
# 4. Exposure cap overrides the signal
# --------------------------------------------------------------------------- #
def test_max_open_exposure_overrides_valid_signal():
    # Existing exposure 4,995,000 + this order's 10,000 > 5,000,000 cap.
    gate = _gate(max_open_exposure=5_000_000)
    decision = gate.check(_signal(), open_exposure=4_995_000)
    _assert_rejected(decision, "max_open_exposure")


def test_open_exposure_within_cap_approves():
    gate = _gate(max_open_exposure=5_000_000)
    decision = gate.check(_signal(), open_exposure=100_000)
    _check(decision.approved is True,
           f"in-cap exposure must not block: {decision.reason}")


# --------------------------------------------------------------------------- #
# 5. Disallowed action overrides the signal
# --------------------------------------------------------------------------- #
def test_disallowed_action_overrides_valid_signal():
    decision = _gate().check(_signal(action="SHORT"))
    _assert_rejected(decision, "allowed_actions")


# --------------------------------------------------------------------------- #
# 6. Layered limits: ALL breaches are reported, still no order
# --------------------------------------------------------------------------- #
def test_multiple_breaches_all_reported():
    gate = RiskGate({
        "blocked": False,
        "max_order_quantity": 50,
        "max_order_notional": 5_000,
        "max_daily_loss": 25_000,
        "allowed_actions": ["BUY", "SELL"],
    })
    decision = gate.check(_signal(quantity=100), day_pnl=-30_000)
    _assert_rejected(decision, "multi-limit")
    _check(len(decision.breaches) >= 3,
           f"quantity + notional + daily-loss should all be reported: "
           f"{decision.breaches}")


def test_unconfigured_limits_never_invent_a_ban():
    """Fail-open on ambiguity: no limits configured means the valid signal
    passes (documented RiskGate contract), even with day_pnl present."""
    decision = check_intent(_signal(), {"allowed_actions": ["BUY", "SELL"]},
                            day_pnl=-30_000)
    _check(decision.approved is True,
           f"no configured limit must not block: {decision.reason}")


# --------------------------------------------------------------------------- #
# 7. Policy-level override: breached drawdown beats a valid book
# --------------------------------------------------------------------------- #
def test_policy_kill_switch_overrides_valid_book():
    """The portfolio policy layer must also refuse to trade a VALID book
    once the drawdown limit is breached."""
    valid_book = {"universe": ["NIFTY", "BANKNIFTY", "IT"], "n_obs": 300,
                  "cov": [[0.020, 0.008, 0.004],
                          [0.008, 0.030, 0.010],
                          [0.004, 0.010, 0.040]]}
    try:
        op.decide(valid_book, regime="RISK_ON", drawdown=0.20)  # breach
        out = op.decide(valid_book, regime="RISK_ON")           # valid book
        _check(out["trade"] is False,
               f"a breached drawdown must override the valid book: {out}")
        _check(out["action"] in ("EXIT_TO_CASH", "HALT"),
               f"action must be a halt, got {out['action']}")
    finally:
        op.reset_kill_switch(confirm="test-cleanup")


# --------------------------------------------------------------------------- #
# Runner (works without pytest)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:        # needs pytest fixtures
                print(f"SKIP {name} (needs pytest fixtures)")
                continue
            fn()
            print(f"PASS {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
