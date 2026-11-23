"""
test_peg.py - PEG-1 passive/peg pricing engine unit tests.

Covers the pure state machine in blue_engine/pov/slicers/peg.py plus the
POVSlicer-level acceptance of the new "PEG" order mode. No SDK, no network,
no sleeps: the PegEngine is driven directly with synthetic TopOfBook
snapshots and explicit timestamps.
"""
from __future__ import annotations

import pytest

from blue_engine.pov.slicers.guards import TopOfBook
from blue_engine.pov.slicers.peg import (
    PegEngine,
    REASON_CROSS_PATIENCE,
    REASON_CROSS_RUNAWAY,
    REASON_HOLD,
    REASON_PASSIVE,
    REASON_REPEG,
    validate_peg_config,
)

TICK = 0.05


def _top(bid: float = 1499.50, ask: float = 1500.50) -> TopOfBook:
    return TopOfBook(best_bid=bid, best_ask=ask, tick_size=TICK, ltp=ask)


def _cfg(**over) -> dict:
    cfg = {"peg_max_chase_ticks": 2, "peg_patience_seconds": 10.0,
           "peg_escape_buffer_ticks": 1}
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------- BUY ----
class TestBuyPegging:
    def test_first_touch_parks_on_bid(self):
        eng = PegEngine(_cfg())
        d = eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=0.0)
        assert d.price == 100.00
        assert d.reason == REASON_PASSIVE

    def test_bid_lift_within_cap_repegs_up(self):
        eng = PegEngine(_cfg())
        eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=0.0)
        d = eng.decide(_top(bid=100.05, ask=100.15), action="BUY", now_ts=1.0)
        assert d.price == 100.05
        assert d.reason == REASON_REPEG

    def test_bid_lift_beyond_cap_crosses_at_ask_plus_buffer(self):
        eng = PegEngine(_cfg(peg_max_chase_ticks=2))
        eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=0.0)
        # Bid jumps 3 ticks past the 2-tick chase cap -> market ran away.
        d = eng.decide(_top(bid=100.15, ask=100.25), action="BUY", now_ts=1.0)
        assert d.reason == REASON_CROSS_RUNAWAY
        assert d.price == 100.25 + TICK  # ask + escape buffer

    def test_patience_expiry_crosses_at_ask_plus_buffer(self):
        eng = PegEngine(_cfg(peg_patience_seconds=10.0))
        eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=0.0)
        d = eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=9.9)
        assert d.reason == REASON_PASSIVE
        d = eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=10.0)
        assert d.reason == REASON_CROSS_PATIENCE
        assert d.price == pytest.approx(100.10 + TICK)

    def test_bid_falling_does_not_chase_down(self):
        eng = PegEngine(_cfg())
        eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=0.0)
        d = eng.decide(_top(bid=99.95, ask=100.05), action="BUY", now_ts=1.0)
        # Never chase the price down; keep the original peg.
        assert d.price == 100.00
        assert d.reason == REASON_PASSIVE

    def test_reset_starts_a_fresh_anchor(self):
        eng = PegEngine(_cfg())
        eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=0.0)
        eng.decide(_top(bid=100.05, ask=100.15), action="BUY", now_ts=1.0)
        eng.reset_for_new_slice()
        d = eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=2.0)
        assert d.price == 100.00
        assert d.reason == REASON_PASSIVE

    def test_chase_accumulates_only_within_cap(self):
        eng = PegEngine(_cfg(peg_max_chase_ticks=2))
        eng.decide(_top(bid=100.00, ask=100.10), action="BUY", now_ts=0.0)
        assert eng.decide(_top(bid=100.05, ask=100.15), action="BUY",
                          now_ts=1.0).price == 100.05
        assert eng.decide(_top(bid=100.10, ask=100.20), action="BUY",
                          now_ts=2.0).price == 100.10  # cap reached, still passive
        # One more tick of lift exceeds the cap -> cross.
        assert eng.decide(_top(bid=100.15, ask=100.25), action="BUY",
                          now_ts=3.0).reason == REASON_CROSS_RUNAWAY


# --------------------------------------------------------------- SELL ----
class TestSellPegging:
    def test_first_touch_parks_on_ask(self):
        eng = PegEngine(_cfg())
        d = eng.decide(_top(bid=100.00, ask=100.10), action="SELL", now_ts=0.0)
        assert d.price == 100.10
        assert d.reason == REASON_PASSIVE

    def test_ask_drop_within_cap_repegs_down(self):
        eng = PegEngine(_cfg())
        eng.decide(_top(bid=100.00, ask=100.10), action="SELL", now_ts=0.0)
        d = eng.decide(_top(bid=99.95, ask=100.05), action="SELL", now_ts=1.0)
        assert d.price == 100.05
        assert d.reason == REASON_REPEG

    def test_ask_drop_beyond_cap_crosses_at_bid_minus_buffer(self):
        eng = PegEngine(_cfg(peg_max_chase_ticks=1))
        eng.decide(_top(bid=100.00, ask=100.10), action="SELL", now_ts=0.0)
        d = eng.decide(_top(bid=99.85, ask=99.90), action="SELL", now_ts=1.0)
        assert d.reason == REASON_CROSS_RUNAWAY
        assert d.price == 99.85 - TICK  # bid - escape buffer

    def test_patience_expiry_crosses_at_bid_minus_buffer(self):
        eng = PegEngine(_cfg(peg_patience_seconds=5.0))
        eng.decide(_top(bid=100.00, ask=100.10), action="SELL", now_ts=0.0)
        d = eng.decide(_top(bid=100.00, ask=100.10), action="SELL", now_ts=5.0)
        assert d.reason == REASON_CROSS_PATIENCE
        assert d.price == 100.00 - TICK

    def test_ask_rising_does_not_chase_up(self):
        eng = PegEngine(_cfg())
        eng.decide(_top(bid=100.00, ask=100.10), action="SELL", now_ts=0.0)
        d = eng.decide(_top(bid=100.05, ask=100.15), action="SELL", now_ts=1.0)
        assert d.price == 100.10
        assert d.reason == REASON_PASSIVE


# ------------------------------------------------------------- edges ----
class TestEdgesAndConfig:
    def test_no_top_of_book_holds(self):
        eng = PegEngine(_cfg())
        d = eng.decide(TopOfBook(ltp=100.0), action="BUY", now_ts=0.0)
        assert d.price is None
        assert d.reason == REASON_HOLD

    def test_bad_action_holds(self):
        eng = PegEngine(_cfg())
        d = eng.decide(_top(), action="HOLD", now_ts=0.0)
        assert d.price is None

    def test_defaults_when_cfg_empty(self):
        eng = PegEngine({})
        assert eng.max_chase_ticks == 2
        assert eng.patience_seconds == 10.0
        assert eng.escape_buffer_ticks == 1

    def test_garbage_cfg_falls_back_to_defaults(self):
        eng = PegEngine({"peg_max_chase_ticks": "abc",
                         "peg_patience_seconds": None,
                         "peg_escape_buffer_ticks": "x"})
        assert eng.max_chase_ticks == 2
        assert eng.patience_seconds == 10.0
        assert eng.escape_buffer_ticks == 1

    def test_validate_peg_config_rejects_bad_values(self):
        assert validate_peg_config({}) is None
        assert validate_peg_config({"peg_max_chase_ticks": -1}) is not None
        assert validate_peg_config({"peg_patience_seconds": 0}) is not None
        assert validate_peg_config({"peg_escape_buffer_ticks": -2}) is not None
        assert validate_peg_config({"peg_max_chase_ticks": "abc"}) is not None


# ------------------------------------------------- slicer integration ----
class TestPovSlicerPegMode:
    def _slicer(self, order_mode: str):
        from blue_engine.pov.slicers.pov import POVSlicer
        return POVSlicer(client=object(), config={
            "action": "BUY", "quantity": 10, "symbol": "INFY",
            "exchange": "NSE", "order_mode": order_mode,
        })

    def test_peg_mode_accepted(self):
        s = self._slicer("PEG")
        assert s.order_mode == "PEG"

    def test_peg_mode_case_insensitive(self):
        s = self._slicer("peg")
        assert s.order_mode == "PEG"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="order_mode"):
            self._slicer("ICEBERG")
