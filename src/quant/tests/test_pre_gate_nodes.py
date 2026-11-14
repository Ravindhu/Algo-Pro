"""Pre-gate node wiring: session windows and feed health inside PreGate.check.

The two new checks are opt-in by design, so there are two things to prove:
1. Omitting the new arguments reproduces the previous behaviour exactly.
2. Supplying them blocks an entry, and never blocks an exit.
"""
from datetime import date, datetime

from quant.core.types import TradeIntent
from quant.monitor.feed_health import FeedHealthBook
from quant.risk.pre_gate import PreGate, PreGateConfig

DAY = date(2026, 11, 13)  # a Friday, not in any fixture holiday set


def _intent(**over):
    base = dict(intent_id="I1", strategy_id="s", symbol="RELIANCE", side="BUY",
                qty=10, ref_price=100.0, kind="entry", lot_size=1, trade_day=DAY)
    base.update(over)
    return TradeIntent(**base)


def _gate(**cfg):
    # holidays=set() keeps the check deterministic: no CSV read, no holiday hit.
    return PreGate(PreGateConfig(**cfg), holidays=set())


# --------------------------------------------------------------------------- #
# back-compat: the new checks must be opt-in
# --------------------------------------------------------------------------- #
def test_check_without_new_arguments_is_unchanged():
    """An existing caller passing no clock and no feed book sees no new refusal."""
    res = _gate().check(_intent())
    assert res.ok is True, res.reasons


def test_exit_is_never_pre_gated_even_with_a_paused_feed():
    book = FeedHealthBook(threshold=1)
    book.record_failure("RELIANCE", "stale")
    res = _gate().check(_intent(kind="exit", side="SELL"), now=datetime(2026, 11, 13, 11, 0),
                        feed_book=book)
    assert res.ok is True, res.reasons


# --------------------------------------------------------------------------- #
# session window
# --------------------------------------------------------------------------- #
def test_entry_inside_auction_window_is_refused():
    gate = _gate(auction_windows="09:15-09:45")
    res = gate.check(_intent(), now=datetime(2026, 11, 13, 9, 20))
    assert res.ok is False
    assert any("session:" in r for r in res.reasons)


def test_entry_outside_auction_window_passes():
    gate = _gate(auction_windows="09:15-09:45")
    res = gate.check(_intent(), now=datetime(2026, 11, 13, 11, 0))
    assert res.ok is True, res.reasons


def test_entry_just_before_window_is_refused_by_buffer():
    gate = _gate(auction_windows="09:15-09:45", auction_buffer_min=2)
    res = gate.check(_intent(), now=datetime(2026, 11, 13, 9, 14))
    assert res.ok is False
    assert any("approaching" in r or "session:" in r for r in res.reasons)


def test_cash_default_does_not_block_a_mid_session_entry():
    """The engine's default schedule is the SME all-day window; cash must not inherit it."""
    res = _gate().check(_intent(), now=datetime(2026, 11, 13, 11, 0))
    assert res.ok is True, res.reasons


def test_sme_flag_blocks_the_whole_session():
    res = _gate(is_sme=True).check(_intent(), now=datetime(2026, 11, 13, 11, 0))
    assert res.ok is False


# --------------------------------------------------------------------------- #
# feed health
# --------------------------------------------------------------------------- #
def test_paused_feed_blocks_that_symbol():
    book = FeedHealthBook(threshold=1)
    book.record_failure("RELIANCE", "dead")
    res = _gate().check(_intent(), feed_book=book)
    assert res.ok is False
    assert any("feed:" in r for r in res.reasons)


def test_paused_feed_blocks_only_its_own_symbol():
    book = FeedHealthBook(threshold=1)
    book.record_failure("OTHER", "dead")
    res = _gate().check(_intent(symbol="RELIANCE"), feed_book=book)
    assert res.ok is True, res.reasons


def test_healthy_feed_passes():
    book = FeedHealthBook(threshold=3)
    book.record_success("RELIANCE")
    assert _gate().check(_intent(), feed_book=book).ok is True


def test_both_checks_together_report_both_reasons():
    book = FeedHealthBook(threshold=1)
    book.record_failure("RELIANCE", "dead")
    res = _gate(auction_windows="09:15-09:45").check(
        _intent(), now=datetime(2026, 11, 13, 9, 20), feed_book=book)
    assert res.ok is False
    text = "; ".join(res.reasons)
    assert "session:" in text and "feed:" in text
