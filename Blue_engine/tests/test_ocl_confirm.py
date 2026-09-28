"""
test_ocl_confirm.py - tests for the keylevel momentum-confirmation layer.

Covers the three new pieces plus their wiring into OCLKeylevelSignal:
    1. alpha/confirm/roc.py    - z-scored ROC and flip detection
    2. alpha/confirm/state.py  - IDLE -> ARMED -> CONFIRMED machine
    3. core/band.py            - BuyerInitiatedDailyVWAP accumulator
    4. ocl_keylevel.py         - the gate, and that it stays OFF by default

No SDK, no network, no sleeps: the signal's market-data methods are driven
through a stub client and the math is exercised directly.
"""
from __future__ import annotations

import pytest

from blue_engine.alpha.confirm.roc import (
    find_flip,
    rate_of_change,
    zscored_roc,
)
from blue_engine.alpha.confirm.state import (
    STATE_ARMED,
    STATE_CONFIRMED,
    STATE_IDLE,
    ConfirmMachine,
)
from blue_engine.core.band import BuyerInitiatedDailyVWAP


# ============================================================ ROC ==========
class TestRateOfChange:
    def test_first_period_bars_are_none(self):
        roc = rate_of_change([100, 101, 102, 103], period=2)
        assert roc[0] is None and roc[1] is None
        assert roc[2] == pytest.approx(2.0)      # 102 vs 100
        assert roc[3] == pytest.approx(103 / 101 * 100 - 100)

    def test_zero_previous_close_yields_none(self):
        """A zero baseline must not raise or produce inf."""
        roc = rate_of_change([0.0, 100.0], period=1)
        assert roc[1] is None

    def test_non_positive_period_rejected(self):
        with pytest.raises(ValueError):
            rate_of_change([1, 2, 3], period=0)


class TestZscoredRoc:
    def test_flat_series_has_no_zscore(self):
        """Zero variance -> undefined z, must be None not division-by-zero."""
        z = zscored_roc([100.0] * 40, period=2, z_window=10)
        assert all(v is None for v in z)

    def test_warmup_is_none_then_values_appear(self):
        closes = [100 + i * 0.1 for i in range(60)]
        z = zscored_roc(closes, period=2, z_window=20)
        # Need roc_period + z_window bars before the first z-score exists.
        assert all(v is None for v in z[:21])
        assert any(v is not None for v in z[22:])

    def test_zscore_uses_no_future_data(self):
        """Truncating the series must not change earlier z values.

        This is the no-look-ahead guarantee. If a value changed when later
        bars were appended, the z-score would be peeking forward.
        """
        closes = [100 + (i % 7) * 0.3 for i in range(80)]
        full = zscored_roc(closes, period=3, z_window=15)
        head = zscored_roc(closes[:60], period=3, z_window=15)
        for i in range(60):
            assert full[i] == head[i]

    def test_non_positive_window_rejected(self):
        with pytest.raises(ValueError):
            zscored_roc([1.0, 2.0], period=1, z_window=0)


class TestFindFlip:
    def test_detects_bullish_cross(self):
        z = [0.0, 0.5, 1.4, 1.6]
        assert find_flip(z, 0, entry_z=1.0, direction="BUY") == 2

    def test_level_test_is_not_a_flip(self):
        """Already above entry_z (no crossing) must NOT count."""
        z = [2.0, 2.5, 3.0]
        assert find_flip(z, 0, entry_z=1.0, direction="BUY") is None

    def test_detects_bearish_cross(self):
        z = [0.0, -0.4, -1.3, -1.8]
        assert find_flip(z, 0, entry_z=1.0, direction="SELL") == 2

    def test_start_idx_respected(self):
        """A flip before the touch bar must be ignored."""
        z = [0.0, 0.1, 1.5, 1.6, 1.7]
        assert find_flip(z, 3, entry_z=1.0, direction="BUY") is None

    def test_none_values_are_skipped(self):
        z = [None, None, 0.5, 1.4]
        assert find_flip(z, 0, entry_z=1.0, direction="BUY") == 3

    def test_bad_direction_rejected(self):
        with pytest.raises(ValueError):
            find_flip([0.0, 1.0], 0, direction="SIDEWAYS")

    def test_non_positive_entry_z_rejected(self):
        with pytest.raises(ValueError):
            find_flip([0.0, 1.0], 0, entry_z=0.0)


# ====================================================== STATE MACHINE ======
class TestConfirmMachine:
    def test_starts_idle(self):
        m = ConfirmMachine()
        assert m.state == STATE_IDLE
        assert m.is_idle()

    def test_arm_transitions_to_armed(self):
        m = ConfirmMachine()
        assert m.arm(level=100.0, action="BUY", bar_idx=5) is True
        assert m.state == STATE_ARMED
        assert m.s.level == 100.0 and m.s.arm_idx == 5

    def test_second_arm_is_ignored_first_touch_only(self):
        """A touch while already armed must NOT re-arm the setup."""
        m = ConfirmMachine()
        m.arm(level=100.0, action="BUY", bar_idx=5)
        assert m.arm(level=101.0, action="BUY", bar_idx=9) is False
        assert m.s.level == 100.0 and m.s.arm_idx == 5

    def test_confirm_requires_arm(self):
        """A flip with no touch behind it must not fire."""
        m = ConfirmMachine()
        assert m.confirm(z_value=1.5, bar_idx=3) is False
        assert m.state == STATE_IDLE

    def test_confirm_records_bars_waited(self):
        m = ConfirmMachine()
        m.arm(level=100.0, action="BUY", bar_idx=5)
        assert m.confirm(z_value=1.7, bar_idx=12) is True
        assert m.state == STATE_CONFIRMED
        assert m.s.bars_waited == 7
        assert m.s.confirm_z == 1.7

    def test_no_bar_discard_operator_rule(self):
        """A setup stays ARMED no matter how long it waits."""
        m = ConfirmMachine()
        m.arm(level=100.0, action="BUY", bar_idx=0)
        assert m.confirm(z_value=1.2, bar_idx=5000) is True
        assert m.state == STATE_CONFIRMED

    def test_reset_returns_to_idle_and_clears(self):
        m = ConfirmMachine()
        m.arm(level=100.0, action="BUY", bar_idx=5)
        m.confirm(z_value=1.5, bar_idx=8)
        m.reset()
        assert m.state == STATE_IDLE
        assert m.s.level is None and m.s.confirm_z is None

    def test_snapshot_is_json_safe(self):
        import json
        m = ConfirmMachine()
        m.arm(level=100.0, action="SELL", bar_idx=2)
        m.confirm(z_value=-1.4, bar_idx=6)
        json.dumps(m.snapshot())          # must not raise


# ================================ BUYER-INITIATED DAILY VWAP ========
class TestBuyerInitiatedDailyVWAP:
    """The daily band counts ONLY buyer-initiated volume.

    Classification mirrors the POV slicer: ltp >= ask. The fail-closed
    behaviour when a candle cannot be classified is tested explicitly,
    because silently counting every candle would turn this into a plain
    VWAP that is then mislabelled as buyer-initiated.
    """

    def test_inactive_before_reset(self):
        acc = BuyerInitiatedDailyVWAP()
        assert not acc.is_active
        assert acc.vwap is None

    def test_quote_rule_counts_ltp_at_or_above_ask(self):
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=10)
        # ltp >= ask -> buyer-initiated -> counts
        assert acc.add_daily_candle(price=101.0, volume=500,
                                    ltp=101.0, ask=101.0) is True
        assert acc.vwap == pytest.approx(101.0)

    def test_quote_rule_excludes_ltp_below_ask(self):
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=10)
        # ltp < ask -> seller-initiated -> must NOT count
        assert acc.add_daily_candle(price=99.0, volume=900,
                                    ltp=99.0, ask=99.5) is False
        assert acc.vwap is None
        assert acc.count == 0

    def test_seller_volume_does_not_dilute_buyer_vwap(self):
        """A big seller day must not move the buyer VWAP."""
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=0)
        acc.add_daily_candle(price=100.0, volume=100,
                             ltp=100.0, ask=100.0)      # counts
        acc.add_daily_candle(price=50.0, volume=10_000,
                             ltp=50.0, ask=50.5)        # excluded
        assert acc.vwap == pytest.approx(100.0)
        assert acc.total_volume == pytest.approx(100.0)

    def test_explicit_flag_when_quote_data_absent(self):
        """OHLCV-only path: the caller supplies the classification."""
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=0)
        acc.add_daily_candle(price=100.0, volume=100, buyer_initiated=True)
        acc.add_daily_candle(price=200.0, volume=100, buyer_initiated=False)
        assert acc.vwap == pytest.approx(100.0)
        assert acc.count == 1

    def test_unclassifiable_candle_fails_closed(self):
        """No ltp/ask AND no flag -> must NOT be counted."""
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=0)
        assert acc.add_daily_candle(price=100.0, volume=100) is False
        assert acc.vwap is None

    def test_zero_volume_ignored(self):
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=0)
        acc.add_daily_candle(price=999.0, volume=0, buyer_initiated=True)
        acc.add_daily_candle(price=100.0, volume=10, buyer_initiated=True)
        assert acc.vwap == pytest.approx(100.0)
        assert acc.count == 1

    def test_vwap_is_volume_weighted(self):
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=0)
        acc.add_daily_candle(price=100.0, volume=100, buyer_initiated=True)
        acc.add_daily_candle(price=102.0, volume=300, buyer_initiated=True)
        assert acc.vwap == pytest.approx(101.5)

    def test_reset_discards_prior_flow(self):
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=0)
        acc.add_daily_candle(price=100.0, volume=1000, buyer_initiated=True)
        acc.reset(level=105.0, action="BUY", start_bar=20)
        acc.add_daily_candle(price=105.0, volume=10, buyer_initiated=True)
        assert acc.vwap == pytest.approx(105.0)
        assert acc.total_volume == pytest.approx(10.0)

    def test_bars_since_touch(self):
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=40)
        assert acc.bars_since_touch(60) == 20
        assert BuyerInitiatedDailyVWAP().bars_since_touch(60) is None

    def test_deviation_pct_sign(self):
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="BUY", start_bar=0)
        acc.add_daily_candle(price=100.0, volume=100, buyer_initiated=True)
        assert acc.deviation_pct(101.0) == pytest.approx(1.0)
        assert acc.deviation_pct(99.0) == pytest.approx(-1.0)

    def test_snapshot_declares_classification(self):
        """The snapshot must state how the VWAP was classified, so no
        downstream reader can mistake a proxy for real tape data."""
        import json
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=100.0, action="SELL", start_bar=1)
        acc.add_daily_candle(price=100.0, volume=5, buyer_initiated=True)
        snap = acc.snapshot()
        assert "buyer-initiated" in snap["classification"]
        json.dumps(snap)          # must be serialisable


# ================================================= SIGNAL INTEGRATION =====
class _StubClient:
    """Minimal SDK stub: returns one LTF frame per call, then repeats it."""

    def __init__(self, frame):
        self._frame = frame
        self.calls = 0

    def history(self, **kwargs):
        self.calls += 1
        return {"status": "success", "data": self._frame}


def _make_signal(monkeypatch, tmp_path, **over):
    """Build an OCLKeylevelSignal whose data access is fully stubbed."""
    import pandas as pd
    from blue_engine.alpha.signals.ocl_keylevel import OCLKeylevelSignal

    # A frame long enough for roc_period + z_window z-score warmup.
    n = 160
    closes = [100.0 + i * 0.05 for i in range(n)]
    frame = pd.DataFrame({
        "open": [c - 0.02 for c in closes],
        "high": [c + 0.10 for c in closes],
        "low": [c - 0.10 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })

    cfg = {
        "symbol": "TEST", "exchange": "NSE",
        "action": "BUY", "quantity": 10,
        "htf_interval": "15minute", "ltf_interval": "5minute",
        "poll_seconds": 0,
        "trigger_price": 100.0,
    }
    cfg.update(over)

    client = _StubClient(frame)
    sig = OCLKeylevelSignal(client, cfg)
    # Bypass the HTF scan: set the level directly so the test drives the gate.
    sig._pattern_detected = True
    sig._trigger_level = float(cfg.get("trigger_level", closes[-1] - 5.0))
    return sig, frame


class TestSignalGatingReal:
    """Gate behaviour with a level the synthetic price actually touches."""

    def _sig(self, tmp_path, **over):
        import pandas as pd
        from blue_engine.alpha.signals.ocl_keylevel import OCLKeylevelSignal

        n = 160
        closes = [100.0 + i * 0.05 for i in range(n)]
        frame = pd.DataFrame({
            "open": [c - 0.02 for c in closes],
            "high": [c + 0.10 for c in closes],
            "low": [c - 0.10 for c in closes],
            "close": closes,
            "volume": [1000] * n,
        })
        cfg = {
            "symbol": "TEST", "exchange": "NSE",
            "action": "BUY", "quantity": 10,
            "htf_interval": "15minute", "ltf_interval": "5minute",
            "poll_seconds": 0,
        }
        cfg.update(over)
        sig = OCLKeylevelSignal(_StubClient(frame), cfg)
        sig._pattern_detected = True
        sig._trigger_level = closes[-1]      # price exactly AT the level
        return sig, frame

    def test_confirmation_disabled_by_default(self, tmp_path):
        sig, _ = self._sig(tmp_path)
        assert sig.confirm_enabled is False
        assert sig._confirm is None

    def test_confirm_flag_creates_state(self, tmp_path):
        sig, _ = self._sig(tmp_path, confirm_enabled=True)
        assert sig.confirm_enabled is True
        assert sig._confirm is not None
        assert sig._touch_vwap is not None

    def test_bad_roc_params_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            self._sig(tmp_path, confirm_enabled=True, roc_period=0)
        with pytest.raises(ValueError):
            self._sig(tmp_path, confirm_enabled=True, entry_z=0)
        with pytest.raises(ValueError):
            self._sig(tmp_path, confirm_enabled=True, z_window=0)

    def test_touch_arms_without_firing(self, tmp_path):
        """Touch -> ARMED, and no intent is returned on that poll."""
        from blue_engine.alpha.confirm.state import STATE_ARMED
        sig, frame = self._sig(tmp_path, confirm_enabled=True)
        ohlc = sig._extract_ohlc(frame.iloc[-1])
        result = sig._monitor_with_confirmation(frame, ohlc, ohlc["close"])
        assert result is None                    # armed, not fired
        assert sig._confirm.state == STATE_ARMED
        assert sig._touch_vwap.is_active

    def test_legacy_mode_still_fires_on_touch(self, tmp_path):
        """Backward compatibility: confirm off -> immediate fire on touch."""
        sig, frame = self._sig(tmp_path)          # confirm_enabled defaults False
        ohlc = sig._extract_ohlc(frame.iloc[-1])
        assert sig._check_touch(ohlc["close"], ohlc) is True
        intent = sig._fire_intent(ohlc["close"])
        assert intent["action"] == "BUY"
        assert "confirm" not in intent["_meta"]

    def test_confirmed_intent_carries_metadata(self, tmp_path):
        """After a flip, the intent must record the confirmation evidence."""
        from blue_engine.alpha.confirm.state import STATE_CONFIRMED
        sig, frame = self._sig(tmp_path, confirm_enabled=True)
        ohlc = sig._extract_ohlc(frame.iloc[-1])
        # Arm, then confirm manually to reach the fire path deterministically.
        sig._bar_idx += 1
        sig._confirm.arm(level=sig._trigger_level, action="BUY", bar_idx=1)
        sig._touch_vwap.reset(level=sig._trigger_level, action="BUY", start_bar=1)
        sig._touch_vwap.add_daily_candle(
            price=ohlc["close"], volume=1000, buyer_initiated=True)
        sig._confirm.confirm(z_value=1.42, bar_idx=6)
        assert sig._confirm.state == STATE_CONFIRMED

        intent = sig._fire_intent(ohlc["close"])
        c = intent["_meta"]["confirm"]
        assert c["roc_z"] == pytest.approx(1.42)
        assert c["bars_waited"] == 5
        assert c["invalidation"] == pytest.approx(sig._trigger_level)
        band = intent["_meta"]["band"]
        assert band["buyer_vwap"] == pytest.approx(ohlc["close"])
        assert band["interval"] == "day"
        assert band["ceiling_price"] > band["buyer_vwap"]
        assert "buyer-initiated" in band["classification"]
        assert "confirmed" in intent["reason"]

    def test_ceiling_gate_stops_slicing(self, tmp_path):
        """Price above the daily band ceiling must halt the gate."""
        sig, _ = self._sig(tmp_path, confirm_enabled=True)
        sig._touch_vwap.reset(level=100.0, action="BUY", start_bar=0)
        sig._touch_vwap.add_daily_candle(
            price=100.0, volume=100, buyer_initiated=True)
        sig.band_ceiling_pct = 0.5
        assert sig._band_ceiling_price() == pytest.approx(100.5)
        assert sig._above_band_ceiling(100.4) is False
        assert sig._above_band_ceiling(100.6) is True

    def test_ceiling_unmeasurable_does_not_halt(self, tmp_path):
        """No daily-buyer VWAP -> no information -> must NOT halt."""
        sig, _ = self._sig(tmp_path, confirm_enabled=True)
        assert sig._band_ceiling_price() is None
        assert sig._above_band_ceiling(999_999.0) is False

    def test_buyer_proxy_classification(self, tmp_path):
        """OHLCV-only proxy: close in the upper half of the range."""
        sig, _ = self._sig(tmp_path, confirm_enabled=True)
        assert sig._classify_buyer_initiated(
            {"open": 100, "high": 110, "low": 100, "close": 109}, None) is True
        assert sig._classify_buyer_initiated(
            {"open": 110, "high": 110, "low": 100, "close": 101}, None) is False

    def test_insufficient_bars_for_zscore_is_safe(self, tmp_path):
        """Too-short frame -> no flip signal, no crash, no fire."""
        import pandas as pd
        sig, _ = self._sig(tmp_path, confirm_enabled=True)
        short = pd.DataFrame({
            "close": [100.0, 100.1, 100.2],
            "high": [100.2, 100.3, 100.4],
            "low": [99.9, 100.0, 100.1],
            "open": [100.0, 100.1, 100.2],
            "volume": [10, 10, 10],
        })
        assert sig._roc_z_series(short) == []

    def test_fire_resets_confirmation_state(self, tmp_path):
        """A fired setup must leave clean state for the next one."""
        from blue_engine.alpha.confirm.state import STATE_IDLE
        sig, frame = self._sig(tmp_path, confirm_enabled=True)
        ohlc = sig._extract_ohlc(frame.iloc[-1])
        sig._confirm.arm(level=sig._trigger_level, action="BUY", bar_idx=1)
        sig._touch_vwap.reset(level=sig._trigger_level, action="BUY", start_bar=1)
        sig._touch_vwap.add_daily_candle(
            price=ohlc["close"], volume=500, buyer_initiated=True)
        sig._fire_intent(ohlc["close"])
        assert sig._confirm.state == STATE_IDLE
        assert not sig._touch_vwap.is_active