"""Stage 7 (Session guards): index circuit breaker and square-off.

The market-halt handler had ZERO implementation - v5 2.7 listed it and nothing
served it. `squareoff.py` existed with a placeholder cutoff and a false-wrap
header, and was called only by tests.
"""
from datetime import date, datetime, time

import pytest

from quant.core.types import TradeIntent
from quant.risk.market_halt import (BANDS, band_table, evaluate_halt,
                                    halt_blocks_entries)
from quant.risk.pre_gate import PreGate, PreGateConfig
from quant.risk.squareoff import (DEFAULT_MIS_CUTOFF, roll_due, roll_urgency,
                                  should_square_off)

DAY = date(2026, 11, 13)


def _at(h, m=0):
    return datetime(2026, 11, 13, h, m)


# --------------------------------------------------------------------------- #
# circuit breaker: the NSE band rules
# --------------------------------------------------------------------------- #
def test_small_move_leaves_the_market_open():
    assert evaluate_halt(-3.0, _at(10)).state == "open"


def test_ten_percent_halts_for_45_minutes_plus_preopen():
    state = evaluate_halt(-10.5, _at(10))
    assert state.state == "halted"
    assert state.band_pct == 10.0
    assert state.resume_at == _at(11, 0)      # 45m halt + 15m pre-open


def test_fifteen_percent_halts_longer():
    state = evaluate_halt(15.2, _at(10))
    assert state.state == "halted"
    assert state.resume_at == _at(12, 0)      # 105m + 15m


def test_twenty_percent_never_resumes_that_day():
    assert evaluate_halt(-20.5, _at(9, 30)).state == "no_resume"


def test_ten_percent_at_or_after_13_does_not_resume():
    """NSE does not resume a band breach at or after 13:00."""
    assert evaluate_halt(-10.0, _at(13, 0)).state == "no_resume"
    assert evaluate_halt(-10.0, _at(14, 30)).state == "no_resume"


def test_ten_percent_at_12_59_still_resumes():
    assert evaluate_halt(-10.0, _at(12, 59)).state in ("halted", "no_resume")


def test_unknown_move_is_reported_as_unknown():
    assert evaluate_halt(None, _at(10)).state == "unknown"


def test_unknown_blocks_entries_by_default():
    """Assuming 'not halted' is how an order reaches a frozen market."""
    state = evaluate_halt(None, _at(10))
    assert state.state == "unknown"
    blocked, reason = halt_blocks_entries(state, _at(10))
    assert blocked is True
    assert reason     # the caller is told WHY it was blocked


def test_unknown_can_be_allowed_explicitly():
    blocked, _ = halt_blocks_entries(evaluate_halt(None, _at(10)), _at(10),
                                     blocked_unknown=False)
    assert blocked is False


# --------------------------------------------------------------------------- #
# circuit breaker: the blocking decision
# --------------------------------------------------------------------------- #
def test_halted_blocks_before_the_resume_time():
    state = evaluate_halt(-10.5, _at(10))
    assert halt_blocks_entries(state, _at(10, 30))[0] is True


def test_halt_lifts_at_the_resume_time():
    state = evaluate_halt(-10.5, _at(10))
    blocked, reason = halt_blocks_entries(state, _at(11, 0))
    assert blocked is False
    assert "lifted" in reason


def test_no_resume_blocks_for_the_rest_of_the_day():
    state = evaluate_halt(-20.5, _at(9, 30))
    blocked, reason = halt_blocks_entries(state, _at(15, 0))
    assert blocked is True
    assert "no resumption" in reason


def test_band_table_matches_the_bands():
    table = band_table()
    assert len(table) == len(BANDS)
    assert table[0]["band_pct"] == 10.0


# --------------------------------------------------------------------------- #
# pre-gate wiring
# --------------------------------------------------------------------------- #
def _intent(kind="entry", side="BUY"):
    return TradeIntent(intent_id="I1", strategy_id="s", symbol="SBIN", side=side,
                       qty=10, ref_price=100.0, kind=kind, lot_size=1, trade_day=DAY)


def test_pre_gate_blocks_an_entry_during_a_halt():
    gate = PreGate(PreGateConfig(), holidays=set())
    res = gate.check(_intent(), index_change_pct=-10.5, now=_at(10))
    assert res.ok is False
    assert any("market halt" in r for r in res.reasons)


def test_halt_length_is_measured_from_the_breach_not_the_poll():
    """A caller polling every cycle must not restart the halt on each poll.

    This is the bug the pre-gate wiring exposed: re-evaluating from scratch made a
    45-minute halt permanent. The breach time is therefore an input.
    """
    breached = _at(10)
    # Polled 70 minutes later with the SAME breach time: the halt is over.
    state = evaluate_halt(-10.5, _at(11, 10), breached_at=breached)
    blocked, _ = halt_blocks_entries(state, _at(11, 10))
    assert blocked is False


def test_polling_without_a_breach_time_restarts_the_halt():
    """Documents the hazard: pass breached_at whenever you poll repeatedly."""
    state = evaluate_halt(-10.5, _at(11, 10))
    assert state.resume_at == _at(12, 10)


def test_pre_gate_ignores_market_state_when_not_supplied():
    """Opt-in: an existing caller keeps its exact behaviour."""
    gate = PreGate(PreGateConfig(), holidays=set())
    assert gate.check(_intent()).ok is True


def test_exits_are_never_blocked_by_a_halt():
    """A stop must still work: a halted market is exactly when you need out."""
    gate = PreGate(PreGateConfig(), holidays=set())
    res = gate.check(_intent("exit", "SELL"), index_change_pct=-20.5, now=_at(10))
    assert res.ok is True


# --------------------------------------------------------------------------- #
# square-off
# --------------------------------------------------------------------------- #
def test_mis_squares_off_at_the_cutoff():
    assert should_square_off(time(15, 15), "MIS") is True
    assert should_square_off(time(15, 14), "MIS") is False


def test_product_is_normalised():
    """' mis' or 'Intraday' must not slip past the obligation."""
    assert should_square_off(time(15, 20), " mis ") is True
    assert should_square_off(time(15, 20), "Intraday") is True


def test_delivery_never_squares_off():
    assert should_square_off(time(23, 59), "CNC") is False
    assert should_square_off(time(23, 59), "DELIVERY") is False


def test_bad_inputs_do_not_square_off():
    assert should_square_off(None, "MIS") is False
    assert should_square_off(time(15, 20), None) is False


def test_default_cutoff_is_before_the_close():
    assert DEFAULT_MIS_CUTOFF < time(15, 30)


# --------------------------------------------------------------------------- #
# rollover
# --------------------------------------------------------------------------- #
def test_roll_due_inside_the_window():
    assert roll_due(date(2026, 11, 14), DAY, roll_days=3) is True


def test_roll_not_due_outside_the_window():
    assert roll_due(date(2026, 11, 30), DAY, roll_days=3) is False


def test_expired_contract_is_not_rolled():
    """An expired contract no longer trades, so 'roll' is the wrong action."""
    assert roll_due(date(2026, 11, 10), DAY, roll_days=3) is False


def test_roll_urgency_reports_expiry_plainly():
    out = roll_urgency(date(2026, 11, 10), DAY)
    assert out["expired"] is True and out["due"] is False


def test_roll_urgency_due():
    out = roll_urgency(date(2026, 11, 15), DAY)
    assert out["due"] is True and out["expired"] is False


def test_negative_roll_window_is_refused():
    with pytest.raises(ValueError):
        roll_due(date(2026, 11, 14), DAY, roll_days=-1)
