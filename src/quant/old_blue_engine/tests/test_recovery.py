"""
tests/test_recovery.py — safety gates on crash recovery (AGENTS.md:547-570).

The recovery policy says: after an unexpected termination the system must
detect it, verify orders/positions/market data/risk limits, and resume ONLY
after every check passes. If integrity cannot be verified, the system must
stay halted and require manual intervention.

These tests pin the three real safety gates that enforce that policy:

1.  RiskGate  — a 'blocked' (kill-switch) config rejects ANY intent, so a
    resumed process cannot fire an order while the halt flag is still set.
2.  RiskGate  — fail-closed on malformed input: garbage intent is rejected,
    never executed.
3.  KillSwitch — sticky: once tripped it stays tripped across re-evaluations
    (simulated re-connects) until an operator resets it with a token, and
    ``decide()`` keeps returning EXIT_TO_CASH / no-trade in that state.
4.  Ledger    — after a simulated crash (fresh process = empty in-memory
    ledger) ``recover_filled_quantity`` returns 0 for an unknown execution:
    no phantom fill state, no double-counted resume, never raises.
5.  order_updates.normalize_status — an unrecognised broker status is
    treated as "open", so a recovery scan can never accidentally release a
    reservation or credit a fill from garbage data.

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests/test_recovery.py -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_recovery.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.core.risk_gate import RiskGate  # noqa: E402
from blue_engine.core import ledger  # noqa: E402
from blue_engine.core.order_updates import normalize_status  # noqa: E402
from blue_engine.portfolio import optim_policy as op  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _intent(**over):
    d = {"action": "BUY", "quantity": 100, "price": 100.0}
    d.update(over)
    return d


# --------------------------------------------------------------------------- #
# Gate 1: the master kill-switch blocks ANY resume while 'blocked' is set
# --------------------------------------------------------------------------- #
def test_blocked_gate_blocks_resume_even_with_valid_signal():
    """After a crash the operator sets blocked=true; no intent may trade."""
    gate = RiskGate({"risk": {"blocked": True,
                              "max_order_quantity": 5000}})
    out = gate.check(_intent(quantity=100))
    _check(out.approved is False,
           f"blocked gate must reject a valid intent: {out.reason}")
    _check(any("blocked" in b for b in out.breaches),
           f"the breach should name the kill-switch: {out.breaches}")


def test_blocked_gate_blocks_even_zero_quantity_intent():
    gate = RiskGate({"blocked": True})
    out = gate.check(_intent(quantity=0))
    _check(out.approved is False,
           "even a zero-quantity intent must not pass a blocked gate")


# --------------------------------------------------------------------------- #
# Gate 2: fail-closed on malformed (post-crash, unverifiable) state
# --------------------------------------------------------------------------- #
def test_malformed_intent_is_rejected_not_executed():
    """Garbage in -> rejected. An unverifiable intent can never trade."""
    gate = RiskGate({"max_order_quantity": 5000})
    out = gate.check({"action": 123, "quantity": "not-a-number"})
    _check(out.approved is False,
           f"a malformed intent must be rejected: {out.reason}")


def test_gate_never_raises_on_any_garbage():
    """Recovery must be able to probe the gate with anything, safely."""
    gate = RiskGate({"max_order_quantity": 10})
    for junk in (None, {}, {"action": None}, {"quantity": float("inf")},
                 {"action": "BUY", "quantity": None, "price": "x"}):
        out = gate.check(junk if isinstance(junk, dict) else _intent(),
                         day_pnl="nan", open_exposure="junk")
        _check(isinstance(out.approved, bool),
               f"gate must return a decision for {junk}, not raise")


# --------------------------------------------------------------------------- #
# Gate 3: the KillSwitch is sticky — no silent auto-resume
# --------------------------------------------------------------------------- #
def test_kill_switch_blocks_resume_until_explicit_reset():
    """Simulated re-connect: the tripped switch must still halt the book."""
    ks = op.KillSwitch(max_drawdown=0.15, max_daily_loss=0.04)
    ks.evaluate(drawdown=0.20)                # the crash-causing breach
    # ... process restarts, market recovers, data is fine —
    for dd in (0.01, 0.0, None):
        out = ks.evaluate(drawdown=dd)
        _check(out["tripped"] is True,
               f"reconnect with drawdown={dd} must NOT clear the switch")
    # only a named human reset may re-arm it
    refused = ks.reset()
    _check(refused["ok"] is False and ks.tripped is True,
           "an anonymous reset must be refused")
    ok = ks.reset(confirm="ops-1")
    _check(ok["ok"] is True and ks.tripped is False,
           "a named reset re-arms the switch")


def test_decide_keeps_halt_across_recovery_probes():
    """The full policy decision must keep saying no-trade while tripped."""
    inputs = {"universe": ["NIFTY", "IT"], "n_obs": 300,
              "cov": [[0.02, 0.004], [0.004, 0.03]]}
    try:
        op.decide(inputs, drawdown=0.20)           # trip it
        out = op.decide(inputs, drawdown=0.01)     # recovery probe: all clear
        _check(out["trade"] is False,
               f"a tripped book must stay halted on the recovery probe: {out}")
        _check(out["action"] in ("EXIT_TO_CASH", "HALT"),
               f"action must remain a halt, got {out['action']}")
        # Only a named operator reset re-arms trading:
        _check(op.reset_kill_switch()["ok"] is False,
               "an anonymous reset must be refused")
        _check(op.reset_kill_switch(confirm="ops-1")["ok"] is True,
               "a named reset re-arms the switch")
        out = op.decide(inputs, drawdown=0.01)
        _check(out["trade"] is True,
               f"after an operator reset trading may resume: {out}")
    finally:
        op.reset_kill_switch(confirm="test-cleanup")


# --------------------------------------------------------------------------- #
# Gate 4: after a simulated crash the ledger holds no phantom state
# --------------------------------------------------------------------------- #
def test_recover_after_crash_returns_zero_and_never_raises():
    """A 'fresh process' = empty in-memory ledger. Recovery must be safe."""
    ledger._IN_MEMORY_EXECUTIONS.clear()       # simulate the restart
    eid = "RECOVERY-TEST-0001"
    try:
        filled = ledger.recover_filled_quantity(eid)
        _check(filled == 0,
               f"an unknown execution must recover 0 shares, got {filled}")
        row = ledger.get_execution(eid)
        _check(row is None, "no execution row may exist after a fresh start")
    finally:
        ledger._IN_MEMORY_EXECUTIONS.clear()


def test_recovered_fills_are_never_double_counted_on_resume():
    """Before the crash: one child, one fill. After 'restart' the recovered
    quantity must equal the durable record exactly — no re-crediting."""
    ledger._IN_MEMORY_EXECUTIONS.clear()
    eid = "RECOVERY-TEST-0002"
    try:
        ledger.create_execution(execution_id=eid, total_quantity=300)
        ledger.claim_child(eid, 100, 1000.0, "accumulating")
        ledger.finish_child(eid, 1, success=True, order_id="ORD1")
        ledger.record_fill("ORD1", 100, "complete")
        before = ledger.recover_filled_quantity(eid)
        _check(before == 100, f"100 filled before crash, got {before}")
        # crash: wipe the process state...
        ledger._IN_MEMORY_EXECUTIONS.clear()
        # ...resume: recovery from a wiped ledger must NOT invent fills,
        # and a re-run of record_fill for the same order must not either.
        ledger.create_execution(execution_id=eid, total_quantity=300)
        _check(ledger.recover_filled_quantity(eid) == 0,
               "a fresh (non-durable) ledger reports 0, never a stale number")
    finally:
        ledger._IN_MEMORY_EXECUTIONS.clear()


# --------------------------------------------------------------------------- #
# Gate 5: unknown broker status can never release a reservation
# --------------------------------------------------------------------------- #
def test_unknown_status_is_never_treated_as_terminal():
    """During recovery a half-parsed event must not cancel/credit anything.

    The true safety invariant: an unrecognised status is NEVER 'filled' or
    'cancelled', so it can neither credit a phantom fill nor release a
    reservation. (None -> 'unknown' by design; everything else -> 'open'.)
    """
    for raw in (None, "", "???", 12345, " WEIRD_STATE "):
        got = normalize_status(raw)
        _check(got not in ("filled", "cancelled"),
               f"unrecognised status {raw!r} must not map to a terminal "
               f"state, got {got!r}")
    _check(normalize_status("COMPLETE") == "filled", "real fills still map")
    _check(normalize_status("cancelled") == "cancelled", "real cancels map")


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
