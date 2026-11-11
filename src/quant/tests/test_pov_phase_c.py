"""Hand-checked tests for POV Phase C (PEG engine and liquidity core). Values worked out by hand."""
import pytest

from quant.execution.pov_guards import TopOfBook
from quant.execution.pov_liquidity import (
    build_liquidity_profile, calculate_max_child_qty, scale_block_size_pct,
)
from quant.execution.pov_peg import (
    PegEngine, REASON_CROSS_PATIENCE, REASON_CROSS_RUNAWAY, REASON_PASSIVE, REASON_REPEG,
)


def _top(bid, ask):
    return TopOfBook(best_bid=bid, bid_qty=100, best_ask=ask, ask_qty=100, ltp=bid,
                     upper_circuit=ask * 1.2, lower_circuit=bid * 0.8, tick_size=0.05,
                     auction_price=None, auction_volume=None, source="depth")


def test_peg_buy_passive_then_repeg_within_cap():
    eng = PegEngine({"peg_max_chase_ticks": 2, "peg_patience_seconds": 10, "peg_escape_buffer_ticks": 1})
    d0 = eng.decide(_top(100.00, 100.10), action="BUY", now_ts=0.0)
    assert (d0.reason, d0.price) == (REASON_PASSIVE, 100.00)
    # Bid lifts one tick. Cap = origin 100.00 + 2 ticks = 100.10, so re-peg to 100.05.
    d1 = eng.decide(_top(100.05, 100.15), action="BUY", now_ts=1.0)
    assert (d1.reason, d1.price) == (REASON_REPEG, 100.05)


def test_peg_buy_patience_expiry_crosses_at_ask_plus_buffer():
    eng = PegEngine({"peg_patience_seconds": 10, "peg_escape_buffer_ticks": 1})
    eng.decide(_top(100.00, 100.10), action="BUY", now_ts=0.0)
    d = eng.decide(_top(100.00, 100.10), action="BUY", now_ts=10.0)
    assert (d.reason, d.price) == (REASON_CROSS_PATIENCE, 100.15)


def test_peg_buy_runaway_crosses_immediately():
    eng = PegEngine({"peg_max_chase_ticks": 2, "peg_patience_seconds": 60, "peg_escape_buffer_ticks": 0})
    eng.decide(_top(100.00, 100.10), action="BUY", now_ts=0.0)
    # Bid 100.20 is past cap 100.10 + half a tick, so the market has run away.
    d = eng.decide(_top(100.20, 100.25), action="BUY", now_ts=1.0)
    assert (d.reason, d.price) == (REASON_CROSS_RUNAWAY, 100.25)


def test_liquidity_tier_and_child_cap_hand_checked():
    # 2,000,000 shares x 150 = 3e8 INR, which is the MID tier (1e8 <= v < 5e8), cap 3%.
    p = build_liquidity_profile("X", "NSE", 2_000_000, 150.0, 1e6)
    assert p.tier_name == "MID"
    assert p.max_block_size_pct == pytest.approx(0.03)
    # pov_qty = 2e6 * 10% = 200,000. liquidity cap = 1000 * 3% = 30. Min of (200000, 30, 1000) = 30.
    assert calculate_max_child_qty(p, remaining_qty=1000, target_qty=1000, participation_pct=10.0) == 30


def test_block_scale_tightens_near_completion():
    p = build_liquidity_profile("X", "NSE", 2_000_000, 150.0, 1e6)
    # Full order: cap 3%, requested 10% becomes 3%.
    assert scale_block_size_pct(p, 1000, 1000, 10.0) == pytest.approx(0.03)
    # Last 5% of order (50 of 1000): cap drops to 2%.
    assert scale_block_size_pct(p, 50, 1000, 10.0) == pytest.approx(0.02)
