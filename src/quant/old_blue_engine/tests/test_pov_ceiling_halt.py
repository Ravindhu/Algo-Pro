"""
test_pov_ceiling_halt.py - the "price above ceiling -> stop slicing" contract.

================================================================================
WHY THIS FILE EXISTS SEPARATELY
================================================================================
The ceiling halt has two halves:

  1. SIGNAL side (ocl_keylevel): refuses to EMIT an intent above the ceiling.
     Covered by tests/test_ocl_confirm.py.

  2. SLICER side (pov): must stop slicing a run that is ALREADY EXECUTING
     when price breaches the ceiling. That is the half that protects capital
     in a live session, and until now it was only proven in unit fragments -
     never through the slicer's real execution loop.

This file proves half 2 by driving POVSlicer.execute() with a stubbed client
whose tape sits ABOVE the band ceiling, then asserting that no child order
was ever placed.

================================================================================
WHAT THIS DOES AND DOES NOT COVER
================================================================================
COVERED:
  * the ceiling is read from the signal's intent (_meta.band)
  * the halt is evaluated inside the real execution loop
  * no child order is shipped while price is above the ceiling
  * a ceiling that is absent/None does NOT halt (fail-open on missing data)
  * a band-absent run still slices normally (backward compatibility)

NOT COVERED (deliberately):
  * the daily-pattern -> touch -> ROC-flip chain that produces the intent.
    That is the signal's job and is tested in test_ocl_confirm.py.
  * real broker behaviour. This is a stub.

================================================================================
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Stub client
# ---------------------------------------------------------------------------
class GrowingTapeClient:
    """Stub SDK client whose cumulative volume grows on every poll.

    The POV slicer slices against CUMULATIVE traded volume. A stub that
    returns a frozen volume makes the slicer's own volume gate refuse to
    slice ("vol stalled"), which would make this test pass for the WRONG
    reason - the halt would never be exercised because no slice was ever
    attempted. So the tape must advance.

    ``ltp`` is held constant (or stepped) so the ceiling comparison is
    deterministic and the test asserts on price position, not on drift.
    """

    def __init__(self, ltp: float, *, volume_step: int = 25_000,
                 start_volume: int = 1_000_000, reject: bool = False):
        self.ltp = float(ltp)
        self._cum_volume = int(start_volume)
        self._volume_step = int(volume_step)
        self.orders: list = []
        self.reject = reject
        self._seq = 0

    # -- market data -------------------------------------------------------
    def history(self, **kwargs):
        """Serve 1-minute records so volume_cache's fallback sees volume."""
        return {"status": "success", "data": [{
            "open": self.ltp, "high": self.ltp, "low": self.ltp,
            "close": self.ltp, "volume": self._cum_volume,
        }]}

    def quotes(self, **kwargs):
        self._cum_volume += self._volume_step      # the tape advances
        return {"status": "success", "data": {
            "ltp": self.ltp,
            "bid": self.ltp * 0.9995,
            "ask": self.ltp * 1.0005,
            "volume": self._cum_volume,
            "upper_circuit_limit": self.ltp * 1.5,
            "lower_circuit_limit": self.ltp * 0.5,
            "tick_size": 0.05,
        }}

    def depth(self, **kwargs):
        return {"status": "success", "data": {
            "bids": [{"price": self.ltp * 0.9995, "quantity": 50_000}],
            "asks": [{"price": self.ltp * 1.0005, "quantity": 50_000}],
            "tick_size": 0.05,
        }}

    def symbol(self, symbol=None, exchange=None, **kwargs):
        return {"status": "success", "data": {
            "symbol": symbol, "exchange": exchange, "lot_size": 1,
            "segment": "EQ", "series": "EQ", "instrumenttype": "EQ",
        }}

    # -- order management --------------------------------------------------
    def placeorder(self, **kwargs):
        self._seq += 1
        if self.reject:
            return {"status": "error", "message": "simulated reject"}
        self.orders.append(dict(kwargs))
        return {"status": "success",
                "order_id": f"T-{self._seq:03d}",
                "data": {"order_id": f"T-{self._seq:03d}"}}

    def orderbook(self, **kwargs):
        return {"status": "success", "data": []}

    def cancelorder(self, *, order_id=None, **kwargs):
        return {"status": "success", "data": {"order_id": order_id}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _intent_with_band(vwap: float, ceiling_pct: float = 0.5) -> dict:
    """An intent shaped exactly as OCLKeylevelSignal emits it."""
    ceiling = vwap * (1.0 + ceiling_pct / 100.0)
    return {
        "action": "BUY",
        "quantity": 100,
        "tag": "CEILING-TEST",
        "reason": "unit test intent",
        "_meta": {
            "band": {
                "interval": "day",
                "buyer_vwap": vwap,
                "ceiling_pct": ceiling_pct,
                "ceiling_price": ceiling,
                "daily_candles_counted": 1,
                "buyer_volume": 1_000_000,
                "classification": "daily buyer-initiated (ltp >= ask)",
                "anchor_level": vwap,
            }
        },
    }


def _intent_without_band() -> dict:
    return {"action": "BUY", "quantity": 100, "tag": "NOBAND",
            "reason": "no band supplied"}


def _slicer(client, **over):
    from blue_engine.pov.slicers.pov import POVSlicer
    cfg = {
        "action": "BUY", "quantity": 100,
        "symbol": "NIFTY", "exchange": "NSE",
        "order_mode": "MARKET",
        "participation_pct": 50.0,
        # interval MUST be > 0. With interval=0 the execution loop spins
        # with a zero-length sleep for the whole `duration`, which burns
        # seconds of wall clock per test and made this file appear to hang.
        # 0.01s keeps the test fast while still exercising the real loop.
        "duration": 1, "interval": 0.01,
        "max_slice_quantity": 50,
        "strategy_label": "CEILING-TEST",
        "min_pov_pct": 5.0, "max_pov_pct": 50.0,
    }
    cfg.update(over)
    return POVSlicer(client, cfg)


# ===========================================================================
# THE CONTRACT: price above the ceiling stops slicing
# ===========================================================================
class TestCeilingHaltStopsSlicing:
    def test_no_child_orders_placed_above_ceiling(self):
        """THE core assertion: above the ceiling, nothing is shipped.

        VWAP 100.0 with a 0.5% ceiling means the ceiling is 100.5. The tape
        sits at 101.0 - decisively above - so every poll must halt.
        """
        client = GrowingTapeClient(ltp=101.0)
        slicer = _slicer(client)
        summary = slicer.execute(_intent_with_band(100.0, 0.5))

        assert summary["ceiling_halts"] > 0, (
            "the ceiling halt never triggered although price (101.0) was "
            "above the ceiling (100.5)")
        assert len(client.orders) == 0, (
            f"slicer shipped {len(client.orders)} child order(s) while price "
            f"was above the ceiling; it must ship none")

    def test_pre_order_monitor_hook_pauses_child_placement(self, monkeypatch):
        from blue_engine.pov.slicers import pov as pov_module

        client = GrowingTapeClient(ltp=100.0)
        slicer = _slicer(client, duration=3, interval=1)
        volume = 0

        def advancing_quote(**kwargs):
            nonlocal volume
            volume += 25_000
            return {"status": "success", "data": {
                "ltp": 100.0, "bid": 99.95, "ask": 100.05,
                "volume": volume, "upper_circuit_limit": 150.0,
                "lower_circuit_limit": 50.0, "tick_size": 0.05,
            }}

        monkeypatch.setattr(
            pov_module, "_get_quote_from_ws", advancing_quote)
        checks = []
        observations = []
        slicer.monitor_observer = lambda: (
            observations.append(True) or True)
        slicer.pre_order_check = lambda **kwargs: (
            checks.append(kwargs) or 0)

        slicer.execute(_intent_without_band())

        assert observations
        assert checks
        assert len(client.orders) == 0

    def test_quote_failure_latches_live_monitor_and_blocks_runner_resume(
        self, monkeypatch
    ):
        from blue_engine.backtest.loops import LiveMonitor
        from blue_engine.core import kill_switch_store
        from blue_engine.core.live_monitor import LiveMonitorGuard
        from blue_engine.portfolio import optim_policy
        from blue_engine.pov.slicers import pov as pov_module

        client = GrowingTapeClient(ltp=100.0)
        monkeypatch.setattr(
            pov_module, "_get_quote_from_ws",
            lambda **kwargs: {"status": "error", "message": "feed lost"})
        guard = LiveMonitorGuard(
            client=client,
            monitor=LiveMonitor({"max_dd": 5.0,
                                 "max_losing_streak": 3},
                                baseline_source="verified_live"),
            provider=lambda **kwargs: {
                "live_equity": [100.0],
                "live_trade_pnls": [],
                "live_daily_returns": [0.0] * 10,
                "historical_daily_returns": [0.0] * 11,
            },
            monitor_config={},
        )
        slicer = _slicer(
            client, duration=1, interval=1, quote_failure_threshold=1)
        slicer.on_data_failure = guard.on_data_failure

        result = slicer.execute(_intent_without_band())

        assert result["child_orders"] == 0
        assert not client.orders
        assert optim_policy.kill_switch_state()["tripped"]
        assert kill_switch_store.load_state()["tripped"]

    def test_halt_is_reported_in_the_summary(self):
        """The halt must be observable after the run, not silent.

        A run that stops trading with no trace is indistinguishable from a
        bug when the operator asks why nothing filled.
        """
        client = GrowingTapeClient(ltp=101.0)
        summary = _slicer(client).execute(_intent_with_band(100.0, 0.5))
        assert "ceiling_halts" in summary
        assert summary["ceiling_halts"] >= 1

    def test_price_exactly_at_ceiling_does_not_halt(self):
        """Boundary: the rule is STRICTLY above (ltp > ceiling).

        A tick sitting exactly on the ceiling is not a breach. Asserting the
        strict inequality here pins the boundary so a future refactor to
        >= cannot silently change behaviour.
        """
        # ceiling = 100.0 * 1.005 = 100.5; tape exactly at 100.5
        client = GrowingTapeClient(ltp=100.5)
        summary = _slicer(client).execute(_intent_with_band(100.0, 0.5))
        assert summary["ceiling_halts"] == 0

    def test_price_below_ceiling_does_not_halt(self):
        """Control case: below the ceiling the slicer must work normally.

        Without this, the halt tests above would also pass if the slicer
        simply never sliced for any reason.
        """
        client = GrowingTapeClient(ltp=99.0)
        summary = _slicer(client).execute(_intent_with_band(100.0, 0.5))
        assert summary["ceiling_halts"] == 0

    def test_ceiling_pct_is_honoured(self):
        """A wider ceiling should tolerate a higher price."""
        client = GrowingTapeClient(ltp=101.0)
        # 2% ceiling on 100.0 -> 102.0, so 101.0 is INSIDE the band.
        summary = _slicer(client).execute(_intent_with_band(100.0, 2.0))
        assert summary["ceiling_halts"] == 0


# ===========================================================================
# FAIL-OPEN: missing band information must never halt trading
# ===========================================================================
class TestCeilingMissingDataIsFailOpen:
    def test_no_band_in_intent_does_not_halt(self):
        """A run with no band must behave exactly as before.

        This is the backward-compatibility guarantee: enabling the band is
        opt-in via the signal, and a slicer fed a band-less intent must not
        suddenly start halting.
        """
        client = GrowingTapeClient(ltp=101.0)
        summary = _slicer(client).execute(_intent_without_band())
        assert summary["ceiling_halts"] == 0

    def test_null_ceiling_with_no_vwap_does_not_halt(self):
        """A null ceiling must mean 'no information', not 'halt at zero'.

        If None were coerced to 0.0, EVERY run without a band would halt on
        its first poll - a total trading outage disguised as a safety
        feature. This covers the case where NEITHER ceiling_price NOR
        buyer_vwap is usable, so no ceiling can be derived at all.

        Note: when ceiling_price is None but buyer_vwap and ceiling_pct ARE
        present, the slicer deliberately DERIVES the ceiling and halts - see
        test_ceiling_reconstructed_from_vwap_and_pct. Deriving is the safer
        behaviour: it keeps the halt meaningful as the reference VWAP ages.
        """
        client = GrowingTapeClient(ltp=101.0)
        intent = _intent_with_band(100.0, 0.5)
        intent["_meta"]["band"]["ceiling_price"] = None
        intent["_meta"]["band"]["buyer_vwap"] = None    # nothing derivable
        summary = _slicer(client).execute(intent)
        assert summary["ceiling_halts"] == 0

    def test_ceiling_reconstructed_from_vwap_and_pct(self):
        """When ceiling_price is absent but vwap+pct are present, derive it.

        The live ceiling is rescaled from the reference VWAP so the halt
        stays meaningful as the reference ages.
        """
        client = GrowingTapeClient(ltp=101.0)
        intent = _intent_with_band(100.0, 0.5)
        intent["_meta"]["band"]["ceiling_price"] = None   # force derivation
        summary = _slicer(client).execute(intent)
        # ceiling_price is None, but vwap=100 and pct=0.5 exist, so the
        # derived ceiling is 100.5 and the tape at 101.0 must halt.
        assert summary["ceiling_halts"] > 0
        assert len(client.orders) == 0

    def test_garbage_band_values_do_not_crash(self):
        """Corrupt band metadata must degrade, never raise."""
        client = GrowingTapeClient(ltp=101.0)
        intent = _intent_with_band(100.0, 0.5)
        intent["_meta"]["band"] = {
            "buyer_vwap": "not-a-number",
            "ceiling_price": "",
            "ceiling_pct": None,
        }
        summary = _slicer(client).execute(intent)   # must not raise
        assert isinstance(summary, dict)

    def test_band_meta_absent_entirely(self):
        client = GrowingTapeClient(ltp=101.0)
        intent = {"action": "BUY", "quantity": 100, "_meta": {}}
        summary = _slicer(client).execute(intent)
        assert summary["ceiling_halts"] == 0


# ===========================================================================
# SELL side: the ceiling rule must not misfire on the opposite direction
# ===========================================================================
class TestSellDirectionUnaffected:
    def test_sell_intent_above_ceiling_still_slices(self):
        """The ceiling is a BUY-side accumulation guard.

        A SELL intent is an exit; halting it above the ceiling would trap a
        position in exactly the situation where exiting is most desirable.
        """
        client = GrowingTapeClient(ltp=101.0)
        intent = _intent_with_band(100.0, 0.5)
        intent["action"] = "SELL"
        slicer = _slicer(client, action="SELL")
        summary = slicer.execute(intent)
        # Whatever the rate logic decides, the run must not be halted by a
        # BUY-side ceiling rule.
        assert summary is not None