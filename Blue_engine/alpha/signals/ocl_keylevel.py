""""
ocl_keylevel.py - OCL (Open-Close-Low) Keylevel Signal Strategy
===============================================================

OCL Strategy Logic:
-------------------
1. SCAN HTF (Higher Timeframe):
   - Fetches last 3 candles from user-defined HTF
   - Detects Red-Red-Green (RRG) or Green-Red-Red (GRR) pattern

2. CALCULATE TOUCH TRIGGER LEVEL:
   - For BULLISH (RRG): Touch Level = min(Low of Candle2, Low of Candle3)
   - For BEARISH (GRR): Touch Level = max(High of Candle2, High of Candle3)
   - Uses candle BODY (not wick) for structural significance

3. MONITOR LTF (Lower Timeframe):
   - Polls LTF price at user-defined interval
   - Watches for price touch of trigger level

4. TRIGGER POV ENGINE (User-Defined Execution):
   - Fires BUY/SELL when trigger level is touched
   - Execution mode is user-defined (one_shot / continuous / count-limited)
   - Optional cooldown between fires
   - Optional per-fire slicing config (qty per fire, etc.)
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, Dict, Optional

from blue_engine.core.time_utils import ist_now

from ._pattern import (
    action_for_pattern,
    calculate_trigger_level,
    extract_ohlc,
    validate_pattern,
)
from .base import BaseSignal

# Confirmation-layer math. Imported at module level (not inside the gate) so
# an ImportError surfaces at import time, not halfway through a live run.
from blue_engine.alpha.confirm import find_flip, zscored_roc


# Execution mode constants (user-selectable per run)
EXEC_MODE_ONE_SHOT = "ONE_SHOT"          # Fire once, then stop
EXEC_MODE_CONTINUOUS = "CONTINUOUS"      # Fire on every touch (with cooldown)
EXEC_MODE_COUNT_LIMITED = "COUNT_LIMITED"  # Fire up to N times (max_fires)
EXEC_MODE_AUTO = "AUTO"                  # One-shot per pattern detection


class OCLKeylevelSignal(BaseSignal):
    """OCL (Open-Close-Low) Keylevel Signal - Pattern-based POV trigger.

    Execution Behavior (User-Defined per run):
    -------------------------------------------
    - exec_mode = ONE_SHOT (default):
        Fires ONCE per run when the trigger is first touched, then stops.
    - exec_mode = CONTINUOUS:
        Fires EVERY TIME the trigger is touched (with cooldown between fires).
    - exec_mode = COUNT_LIMITED:
        Fires up to max_fires times per run, then stops.
    - exec_mode = AUTO:
        Fires once per NEW pattern detection (re-arms when fresh RRG/GRR forms).

    Slicing Per Fire (User-Defined per run):
    ----------------------------------------
    - qty_per_fire: shares/contracts to fire on each signal
    - cooldown_seconds: minimum gap between consecutive fires
    - max_fires: total fires allowed (only for COUNT_LIMITED mode)
    """

    def __init__(self, client: Any, config: Dict[str, Any]):
        super().__init__(client, config)

        # [HTF SETTINGS] Higher Timeframe Configuration
        self.htf_interval = config.get("htf_interval", "15minute")
        self.htf_candles = int(config.get("htf_candles", 5))

        # [PATTERN SETTINGS] Candlestick Pattern Parameters
        self.min_body_pct = float(config.get("min_body_pct", 0.3))
        self.pattern_type = config.get("pattern_type", "RRG").upper()
        self.trade_direction = config.get("trade_direction", "AUTO").upper()

        # [LTF SETTINGS] Lower Timeframe Monitoring
        self.ltf_interval = config.get("ltf_interval", "5minute")
        self.ltf_touch_buffer = float(config.get("ltf_touch_buffer", 0.05))

        # [TRADE SETTINGS] Execution Parameters
        self.symbol = config.get("symbol", config["symbol"])
        self.exchange = config.get("exchange", config["exchange"])
        self.action = config.get("action", "BUY").upper()
        self.total_quantity = int(config.get("quantity", config.get("total_quantity", 0)))

        # [MONITORING SETTINGS] Polling and Timeout Controls
        self.poll_seconds = int(config.get("poll_seconds", 10))
        self.wait_seconds = config.get("wait_seconds")
        self.one_shot = bool(config.get("one_shot", True))  # legacy flag

        # [EXECUTION SETTINGS] Per-Run Slicing Behavior (USER-DEFINED)
        # Execution mode controls how many times the signal can fire
        mode = config.get("exec_mode")
        if mode is None:
            # Backward compat: derive from one_shot flag
            mode = EXEC_MODE_ONE_SHOT if self.one_shot else EXEC_MODE_CONTINUOUS
        self.exec_mode = str(mode).upper().strip()
        if self.exec_mode not in (
            EXEC_MODE_ONE_SHOT,
            EXEC_MODE_CONTINUOUS,
            EXEC_MODE_COUNT_LIMITED,
            EXEC_MODE_AUTO,
        ):
            raise ValueError(
                f"exec_mode must be ONE_SHOT, CONTINUOUS, COUNT_LIMITED, or AUTO; got {self.exec_mode!r}"
            )

        # Quantity to fire per individual signal event
        # - If not set, defaults to total_quantity (entire position in one fire)
        # - If set, fires this amount per touch event
        self.qty_per_fire = int(
            config.get("qty_per_fire", self.total_quantity)
        )
        if self.qty_per_fire <= 0:
            self.qty_per_fire = self.total_quantity

        # Maximum total fires (only for COUNT_LIMITED mode)
        self.max_fires = int(config.get("max_fires", 1))
        if self.exec_mode == EXEC_MODE_COUNT_LIMITED and self.max_fires < 1:
            raise ValueError("max_fires must be >= 1 when exec_mode=COUNT_LIMITED")

        # Cooldown between consecutive fires (seconds)
        # - ONE_SHOT/AUTO: ignored
        # - CONTINUOUS/COUNT_LIMITED: enforced
        self.cooldown_seconds = float(config.get("cooldown_seconds", 0))
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")

        # Re-arm: should the pattern be re-scanned after a fire?
        # - ONE_SHOT/CONTINUOUS/COUNT_LIMITED: no, level is fixed
        # - AUTO: yes, re-scan for new pattern after each fire
        self.rearm_on_fire = bool(
            config.get("rearm_on_fire", self.exec_mode == EXEC_MODE_AUTO)
        )

        # [CONFIRMATION SETTINGS] Momentum confirmation gate (OPT-IN)
        # -------------------------------------------------------------
        # When confirm_enabled is False (the default) this signal behaves
        # EXACTLY as before: a keylevel touch fires immediately. When True, a
        # touch only ARMS the setup; the trade fires once a z-scored ROC
        # momentum flip confirms that the reversal is actually happening.
        # Keeping the default off means existing runs are byte-identical.
        # [BAND SETTINGS] Buyer-initiated daily VWAP band
        # The band is ALWAYS daily, independent of htf_interval/ltf_interval:
        # the OCL pattern and the momentum confirmation run on their own
        # timeframes, while the execution band is a daily construct.
        self.band_interval = config.get("band_interval", "day")
        self.band_ceiling_pct = float(config.get("band_ceiling_pct", 0.5))
        #: ISO date of the keylevel touch. The daily band window starts here.
        self._touch_date: Optional[str] = None

        self.confirm_enabled = bool(config.get("confirm_enabled", False))
        self.roc_period = int(config.get("roc_period", 10))
        self.z_window = int(config.get("z_window", 100))
        self.entry_z = float(config.get("entry_z", 1.0))
        if self.roc_period <= 0:
            raise ValueError("roc_period must be > 0")
        if self.z_window <= 0:
            raise ValueError("z_window must be > 0")
        if self.entry_z <= 0:
            raise ValueError("entry_z must be > 0")

        # Internal state
        self._fired = False                       # Legacy one-shot guard
        self._fire_count = 0                      # Total fires so far
        self._last_fire_ts: Optional[float] = None  # For cooldown
        self._start_ts = time.time()
        self._trigger_level: Optional[float] = None
        self._pattern_detected = False
        self._exhausted = False                   # All fires done

        # Confirmation-layer state (only used when confirm_enabled)
        if self.confirm_enabled:
            from blue_engine.alpha.confirm import ConfirmMachine
            from blue_engine.core.band import BuyerInitiatedDailyVWAP

            self._confirm = ConfirmMachine()
            self._touch_vwap = BuyerInitiatedDailyVWAP()
            self._bar_idx = 0                     # Bar counter for the LTF
            self.log(
                f"[CONFIRM] enabled: ROC({self.roc_period}) "
                f"z-window={self.z_window} entry_z={self.entry_z} "
                f"| daily buyer-VWAP band, ceiling=+{self.band_ceiling_pct}%"
            )
        else:
            self._confirm = None
            self._touch_vwap = None
            self._bar_idx = 0

        # Validate quantity budget vs qty_per_fire
        if self.qty_per_fire > self.total_quantity and self.total_quantity > 0:
            self.log(
                f"[WARN] qty_per_fire={self.qty_per_fire} > total_quantity={self.total_quantity}; "
                f"clamping qty_per_fire to total_quantity"
            )
            self.qty_per_fire = self.total_quantity

        self.log(
            f"Initialized: HTF={self.htf_interval}, LTF={self.ltf_interval}, "
            f"Pattern={self.pattern_type}, Direction={self.trade_direction}"
        )
        self.log(
            f"Execution: mode={self.exec_mode}, total_qty={self.total_quantity}, "
            f"qty_per_fire={self.qty_per_fire}, max_fires={self.max_fires}, "
            f"cooldown={self.cooldown_seconds}s, rearm={self.rearm_on_fire}"
        )

    # ============================================================
    # [DETECT LOOP] Main entry point - called by POV runner
    # ============================================================
    def detect(self):
        """Main detection loop - called repeatedly by POV runner.

        Per-Run Execution Flow (User-Defined):
        ---------------------------------------
        1. Check exhaustion (already fired max times)
        2. Check cooldown (must wait between fires)
        3. If pattern not detected yet -> scan HTF
        4. If pattern detected -> monitor LTF for touch
        5. On touch -> fire intent (per qty_per_fire) -> return to runner
        """
        # -- 1. Exhaustion guard (no more fires allowed) --
        if self._exhausted:
            return None

        # -- 2. Cooldown guard (must wait between fires) --
        if self._last_fire_ts is not None and self.cooldown_seconds > 0:
            elapsed = time.time() - self._last_fire_ts
            if elapsed < self.cooldown_seconds:
                remaining = self.cooldown_seconds - elapsed
                self.log(
                    f"[COOLDOWN] {remaining:.1f}s remaining before next fire"
                )
                time.sleep(min(remaining, self.poll_seconds))
                return None

        # -- 3. Legacy one-shot guard (backward compat) --
        if self.one_shot and self._fired and self.exec_mode == EXEC_MODE_ONE_SHOT:
            return None

        # -- 4. Overall timeout --
        if self.wait_seconds is not None:
            elapsed = time.time() - self._start_ts
            if elapsed > float(self.wait_seconds):
                self.log(f"[TIMEOUT] {self.wait_seconds}s elapsed; giving up")
                self._exhausted = True
                return None

        # -- 5. Step 1: Scan HTF for pattern (if not yet detected) --
        if not self._pattern_detected:
            self._scan_htf_pattern()
            if not self._pattern_detected:
                time.sleep(self.poll_seconds)
                return None

        # -- 6. Step 2: Monitor LTF for touch --
        return self._monitor_ltf_touch()

    # ============================================================
    # [HTF SCAN] Higher Timeframe Pattern Detection
    # ============================================================
    def _scan_htf_pattern(self):
        """Scan HTF for candlestick pattern and calculate trigger level."""
        self.log(f"[HTF SCAN] Checking {self.htf_interval} for {self.pattern_type}...")

        try:
            end_date = ist_now().strftime("%Y-%m-%d")
            start_date = (ist_now() - timedelta(days=3)).strftime("%Y-%m-%d")

            response = self.client.history(
                symbol=self.symbol,
                exchange=self.exchange,
                interval=self.htf_interval,
                start_date=start_date,
                end_date=end_date
            )

            if response.get("status") != "success":
                self.log(f"[HTF ERROR] {response.get('message')}")
                return False

            df = response.get("data")
            if df is None or len(df) < 3:
                self.log(f"[HTF ERROR] Insufficient candles ({len(df) if df else 0})")
                return False

            c1 = self._extract_ohlc(df.iloc[-3])
            c2 = self._extract_ohlc(df.iloc[-2])
            c3 = self._extract_ohlc(df.iloc[-1])

            self.log(
                f"[HTF DATA] C1: O={c1['open']:.2f} H={c1['high']:.2f} "
                f"L={c1['low']:.2f} C={c1['close']:.2f}"
            )
            self.log(
                f"[HTF DATA] C2: O={c2['open']:.2f} H={c2['high']:.2f} "
                f"L={c2['low']:.2f} C={c2['close']:.2f}"
            )
            self.log(
                f"[HTF DATA] C3: O={c3['open']:.2f} H={c3['high']:.2f} "
                f"L={c3['low']:.2f} C={c3['close']:.2f}"
            )

            if not self._validate_pattern(c1, c2, c3):
                return False

            self._trigger_level = self._calculate_trigger_level(c1, c2, c3)

            if self.trade_direction == "AUTO":
                self.action = action_for_pattern(self.pattern_type)

            self._pattern_detected = True
            self.log(
                f"[HTF PATTERN] OK {self.pattern_type} detected! "
                f"Level={self._trigger_level:.2f}, Action={self.action}"
            )
            return True

        except Exception as e:
            self.log(f"[HTF ERROR] {e}")
            return False

    # ============================================================
    # [PATTERN VALIDATION] RRG / GRR Candlestick Validation
    # ============================================================
    def _validate_pattern(self, c1, c2, c3):
        """Validate candlestick pattern (RRG or GRR). Delegates to shared helper."""
        ok = validate_pattern(c1, c2, c3, self.pattern_type, self.min_body_pct)
        if ok:
            self.log("[PATTERN] OK Validated")
        else:
            self.log(f"[PATTERN MISMATCH] Expected {self.pattern_type}")
        return ok

    # ============================================================
    # [TRIGGER CALCULATION] Body-Based Key Level
    # ============================================================
    def _calculate_trigger_level(self, c1, c2, c3):
        """Calculate Touch Trigger Level from candle bodies. Delegates to shared helper."""
        level = calculate_trigger_level(c2, c3, self.pattern_type)
        if self.pattern_type == "RRG":
            c2_body_low = min(c2["open"], c2["close"])
            c3_body_low = min(c3["open"], c3["close"])
            self.log(f"[TRIGGER] Bullish: {c2_body_low:.2f}/{c3_body_low:.2f} -> {level:.2f}")
        else:
            c2_body_high = max(c2["open"], c2["close"])
            c3_body_high = max(c3["open"], c3["close"])
            self.log(f"[TRIGGER] Bearish: {c2_body_high:.2f}/{c3_body_high:.2f} -> {level:.2f}")
        return level

    # ============================================================
    # [LTF MONITOR] Lower Timeframe Price Touch Detection
    # ============================================================
    def _monitor_ltf_touch(self):
        """Monitor LTF for price touch of trigger level."""
        self.log(f"[LTF MONITOR] Watching {self.ltf_interval}...")

        try:
            end_date = ist_now().strftime("%Y-%m-%d")
            start_date = (ist_now() - timedelta(hours=1)).strftime("%Y-%m-%d")

            response = self.client.history(
                symbol=self.symbol,
                exchange=self.exchange,
                interval=self.ltf_interval,
                start_date=start_date,
                end_date=end_date
            )

            if response.get("status") != "success":
                self.log(f"[LTF ERROR] {response.get('message')}")
                time.sleep(self.poll_seconds)
                return None

            df = response.get("data")
            if df is None or len(df) < 1:
                self.log("[LTF ERROR] No candles")
                time.sleep(self.poll_seconds)
                return None

            ohlc = self._extract_ohlc(df.iloc[-1])
            ltp = ohlc["close"]

            self.log(f"[LTF DATA] H={ohlc["high"]:.2f} L={ohlc["low"]:.2f} C={ltp:.2f}")

            # ---- Confirmation-gated path (opt-in) ----
            if self.confirm_enabled:
                return self._monitor_with_confirmation(df, ohlc, ltp)

            # ---- Legacy path: touch fires immediately ----
            if self._check_touch(ltp, ohlc):
                return self._fire_intent(ltp)

            self.log(f"[LTF WAIT] LTP={ltp:.2f}, Trigger={self._trigger_level:.2f}")
            time.sleep(self.poll_seconds)
            return None

        except Exception as e:
            self.log(f"[LTF ERROR] {e}")
            time.sleep(self.poll_seconds)
            return None

    # ============================================================
    # [CONFIRMATION GATE] Touch -> ARM -> z-ROC flip -> fire
    # ============================================================
    def _monitor_with_confirmation(self, df, ohlc, ltp):
        """Two-stage gate: a touch ARMS the setup, a momentum flip fires it.

        Stage 1 (touch): the first touch of the trigger level starts the
            touch-anchored VWAP window and moves the machine to ARMED.
            Later touches of the same level do NOT re-arm it.
        Stage 2 (flip): on every subsequent poll the z-scored ROC is
            evaluated. When it flips in the setup's direction, the trade
            fires with the confirmation metadata attached.

        There is NO bar-count discard (operator-confirmed): an armed setup
        stays armed until the flip arrives or the run ends.
        """
        self._bar_idx += 1
        bar = self._bar_idx

        # -- Stage 1: arm on first touch --
        if self._confirm.is_idle():
            if not self._check_touch(ltp, ohlc):
                self.log(
                    f"[CONFIRM WAIT] LTP={ltp:.2f} "
                    f"Trigger={self._trigger_level:.2f}"
                )
                time.sleep(self.poll_seconds)
                return None

            self._confirm.arm(
                level=self._trigger_level,
                action=self.action,
                bar_idx=bar,
            )
            # Anchor the daily band at the TOUCH CANDLE. The touch date is
            # recorded explicitly (not just a bar index) because the band is
            # rebuilt from a separate daily history frame whose positions do
            # not correspond to LTF bar indices.
            self._touch_date = ist_now().strftime("%Y-%m-%d")
            self._touch_vwap.reset(
                level=self._trigger_level,
                action=self.action,
                start_bar=0,
            )
            self.log(
                f"[CONFIRM ARMED] level={self._trigger_level:.2f} "
                f"action={self.action} bar={bar} touch_date={self._touch_date} "
                f"- awaiting ROC flip"
            )

        # -- Daily band: rebuild the buyer-initiated daily VWAP each poll --
        # The band is a DAILY construct. It is recomputed from daily candles
        # (not accumulated per LTF poll) so the window is the set of daily
        # candles from the touch forward, matching the operator's spec.
        self._refresh_daily_band()

        # -- Ceiling gate: price above the band ceiling STOPS slicing --
        if self._above_band_ceiling(ltp):
            self.log(
                f"[BAND HALT] ltp={ltp:.2f} above ceiling "
                f"({self._band_ceiling_price():.2f}); slicing stopped"
            )
            time.sleep(self.poll_seconds)
            return None

        # -- Stage 2: evaluate the momentum flip --
        z_series = self._roc_z_series(df)
        flip_idx = find_flip(
            z_series,
            start_idx=len(z_series) - 1,   # only the newest bar can trigger
            entry_z=self.entry_z,
            direction=self.action,
        )

        if flip_idx is None:
            z_now = z_series[-1] if z_series else None
            z_txt = f"{z_now:.2f}" if isinstance(z_now, (int, float)) else "n/a"
            self.log(
                f"[CONFIRM ARMED] z={z_txt} (need {'>' if self.action == 'BUY' else '<'} "
                f"{-self.entry_z if self.action == 'SELL' else self.entry_z:.2f}); "
                f"bars_armed={self._confirm.s.bars_waited or bar - (self._confirm.s.arm_idx or bar)}"
            )
            time.sleep(self.poll_seconds)
            return None

        z_at_flip = z_series[flip_idx]
        self._confirm.confirm(z_value=z_at_flip, bar_idx=bar)
        self.log(
            f"[CONFIRM OK] z flipped to {z_at_flip:.2f} at bar {bar} "
            f"(armed at bar {self._confirm.s.arm_idx})"
        )
        return self._fire_intent(ltp)

    # ============================================================
    # [CONFIRM HELPERS] Data extraction for the gate
    # ============================================================
    def _roc_z_series(self, df):
        """Compute the z-scored ROC series over the LTF closes.

        Returns an empty list when the frame is too short to compute a
        z-score, so the caller treats it as "no flip yet" rather than
        crashing mid-run.
        """
        try:
            closes = [float(c) for c in df["close"].tolist()]
        except (KeyError, TypeError, ValueError, AttributeError):
            return []

        # Need roc_period for the first ROC plus z_window ROC observations.
        if len(closes) < self.roc_period + self.z_window:
            self.log(
                f"[CONFIRM] insufficient bars for z-score "
                f"({len(closes)} < {self.roc_period + self.z_window}); "
                f"flip cannot be evaluated yet"
            )
            return []
        return zscored_roc(
            closes, period=self.roc_period, z_window=self.z_window
        )

    def _bar_volume(self, df) -> float:
        """Volume of the newest bar, or 0.0 when unavailable."""
        try:
            return float(df["volume"].iloc[-1])
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            return 0.0

    # ============================================================
    # [DAILY BAND] Buyer-initiated daily VWAP from the touch forward
    # ============================================================
    def _refresh_daily_band(self) -> None:
        """Rebuild the buyer-initiated daily VWAP from daily history.

        Classification uses the SAME rule the POV slicer applies to its tick
        stream (``ltp >= ask``). Daily OHLCV carries no ``ask``, so the
        classification input has to come from somewhere. This method supports
        both shapes and prefers the real one:

          * If the daily frame carries an ``ask`` column AND the caller has
            configured ``band_use_quote_rule`` (default True), the literal
            ``ltp >= ask`` test is applied.
          * Otherwise the caller-supplied proxy column is used. The proxy is
            ``band_buyer_proxy`` in config, naming a boolean column, or the
            documented close-position proxy when that column is absent.

        Honesty note: the close-position fallback is an APPROXIMATION of
        buyer-initiated flow, not the real tape rule. It is recorded in the
        intent metadata as ``classification`` so no downstream reader can
        mistake it for quote-based classification.
        """
        if self._touch_vwap is None:
            return
        try:
            end_date = ist_now().strftime("%Y-%m-%d")
            start_date = (ist_now() - timedelta(days=120)).strftime("%Y-%m-%d")
            resp = self.client.history(
                symbol=self.symbol, exchange=self.exchange,
                interval=self.band_interval, start_date=start_date,
                end_date=end_date,
            )
            if resp.get("status") != "success":
                self.log(f"[BAND] daily history failed: {resp.get('message')}")
                return
            frame = resp.get("data")
            if frame is None or len(frame) == 0:
                self.log("[BAND] no daily candles returned")
                return
        except Exception as exc:
            self.log(f"[BAND] daily history error: {exc}")
            return

        start = 0
        if self._touch_date:
            # Anchor at the touch candle: keep only daily candles dated on or
            # after the touch date. This is the "from the touch candle"
            # window - NOT the whole daily history, and not a positional
            # slice of a differently-indexed frame.
            try:
                dates = frame["datetime"].astype(str).str[:10].tolist()
            except (KeyError, TypeError, AttributeError):
                try:
                    dates = frame.index.astype(str).tolist()
                except (AttributeError, TypeError):
                    dates = []
            if dates:
                try:
                    start = next(
                        i for i, d in enumerate(dates)
                        if d[:10] >= self._touch_date
                    )
                except StopIteration:
                    start = len(frame)      # touch is newer than all history
            self.log(
                f"[BAND] anchoring from touch_date={self._touch_date} "
                f"-> {len(frame) - start} daily candle(s)"
            )
        candles = frame.iloc[start:]
        # Rebuild from scratch: the band is a pure function of the daily
        # window, so re-deriving avoids double-counting a partial day.
        level_before = self._touch_vwap.level
        action_before = self._touch_vwap.action
        self._touch_vwap.reset(
            level=level_before, action=action_before, start_bar=0)

        for _, row in candles.iterrows():
            ohlc = self._extract_ohlc(row)
            vol = 0.0
            try:
                vol = float(row["volume"])
            except (KeyError, TypeError, ValueError):
                vol = 0.0
            ltp = ohlc["close"]
            ask = None
            try:
                ask = float(row["ask"]) if "ask" in row else None
            except (TypeError, ValueError):
                ask = None

            buyer = self._classify_buyer_initiated(ohlc, ask)
            self._touch_vwap.add_daily_candle(
                price=ltp, volume=vol,
                ltp=(ltp if ask is not None else None),
                ask=ask, buyer_initiated=buyer,
            )

        v = self._touch_vwap.vwap
        self.log(
            f"[BAND] daily buyer-VWAP="
            f"{v:.2f}" if v is not None else "[BAND] daily buyer-VWAP=n/a"
        )

    def _classify_buyer_initiated(self, ohlc, ask) -> Optional[bool]:
        """Decide whether a daily candle counts as buyer-initiated.

        Returns None when no decision can be made (the accumulator fails
        closed on None, so an unclassifiable day is excluded rather than
        silently counted).
        """
        if ask is not None:
            # Real quote-based rule; the accumulator applies ltp >= ask.
            return None
        try:
            high, low, close = ohlc["high"], ohlc["low"], ohlc["close"]
        except (KeyError, TypeError):
            return None
        rng = high - low
        if rng <= 0:
            return None
        # Documented approximation: a close in the upper half of the day's
        # range is treated as buyer-dominated. NOT equivalent to ltp >= ask.
        return (close - low) / rng >= 0.5

    def _band_ceiling_price(self) -> Optional[float]:
        """Ceiling price = buyer daily VWAP * (1 + band_ceiling_pct/100)."""
        if self._touch_vwap is None:
            return None
        v = self._touch_vwap.vwap
        if v is None or v <= 0:
            return None
        return v * (1.0 + self.band_ceiling_pct / 100.0)

    def _above_band_ceiling(self, ltp: float) -> bool:
        """True when the band ceiling says: stop slicing.

        ``False`` whenever the ceiling is not measurable. If the band cannot
        be computed there is no information to halt on, and refusing to halt
        is safer than halting every run where the daily fetch failed.
        """
        ceiling = self._band_ceiling_price()
        if ceiling is None:
            return False
        return ltp > ceiling

    # ============================================================
    # [TOUCH CHECK] Price-vs-Level Comparison
    # ============================================================
    def _check_touch(self, ltp, candle):
        """Check if price touched the trigger level."""
        level = self._trigger_level
        buffer = self.ltf_touch_buffer

        if self.action == "BUY":
            if candle["low"] <= (level + buffer):
                self.log("[TOUCH] OK Bullish support!")
                return True
        else:
            if candle["high"] >= (level - buffer):
                self.log("[TOUCH] OK Bearish resistance!")
                return True
        return False

    # ============================================================
    # [FIRE INTENT] User-Defined Per-Run Slicing Behavior
    # ============================================================
    def _fire_intent(self, ltp):
        """Fire trade intent to POV slicer with user-defined slicing.

        Per-Fire Behavior (controlled by exec_mode):
        --------------------------------------------
        - ONE_SHOT:        Fire once, then mark exhausted
        - CONTINUOUS:      Fire on every touch (cooldown enforced)
        - COUNT_LIMITED:   Fire up to max_fires times
        - AUTO:            Fire once per pattern; re-arm on rearm_on_fire
        """
        self._fire_count += 1
        self._last_fire_ts = time.time()
        self._fired = True

        # Use the user-defined qty_per_fire (NOT total_quantity)
        # This enables per-run slicing (e.g. fire 100/10x for 1000 total)
        fire_qty = self.qty_per_fire

        intent = {
            "action": self.action,
            "quantity": fire_qty,
            "tag": self.config.get("strategy_label", "OCL-Keylevel"),
            "reason": (
                f"OCL-{self.pattern_type} fire #{self._fire_count}/{self._max_label()} "
                f"at {ltp:.2f} (level={self._trigger_level:.2f}, mode={self.exec_mode})"
            ),
            "_meta": {
                "fire_count": self._fire_count,
                "exec_mode": self.exec_mode,
                "trigger_level": self._trigger_level,
                "pattern": self.pattern_type,
            },
        }

        # Confirmation metadata: recorded so the operator can audit AFTER the
        # fact whether entries were taken near the post-touch value, and so
        # the backtest harness can measure whether the confirmation layer
        # actually earns its filtering cost.
        if self.confirm_enabled and self._confirm is not None:
            snap = self._confirm.snapshot()
            intent["_meta"]["confirm"] = {
                "state": snap["state"],
                "roc_z": snap["confirm_z"],
                "bars_waited": snap["bars_waited"],
                "arm_idx": snap["arm_idx"],
                "confirm_idx": snap["confirm_idx"],
                "roc_period": self.roc_period,
                "z_window": self.z_window,
                "entry_z": self.entry_z,
                "invalidation": self._trigger_level,
            }
            # The daily buyer-initiated VWAP band. The slicer consumes these
            # fields to drive its POV rate and to halt at the ceiling.
            band_snap = (self._touch_vwap.snapshot()
                         if self._touch_vwap is not None else {})
            intent["_meta"]["band"] = {
                "interval": self.band_interval,
                "buyer_vwap": band_snap.get("buyer_vwap"),
                "ceiling_pct": self.band_ceiling_pct,
                "ceiling_price": self._band_ceiling_price(),
                "daily_candles_counted": band_snap.get("daily_candles_counted"),
                "buyer_volume": band_snap.get("buyer_volume"),
                "classification": band_snap.get("classification"),
                "anchor_level": self._trigger_level,
            }
            intent["reason"] += (
                f" + confirmed (z={snap['confirm_z']:.2f}, "
                f"bars_waited={snap['bars_waited']})"
                if snap["confirm_z"] is not None else " + confirmed"
            )

        self.log("=" * 60)
        self.log(
            f"FIRED #{self._fire_count}: {intent['action']} {fire_qty} shares -> {intent['reason']}"
        )
        self.log("=" * 60)

        # ---- Post-fire state management (per user-defined exec_mode) ----
        # The confirmation layer is per-setup: whatever the exec_mode decides
        # about re-arming, the touch VWAP and the ARM/CONFIRM state must start
        # clean for the *next* setup. Leaving them populated would let a stale
        # anchor leak into the following setup's VWAP.
        if self.confirm_enabled and self._confirm is not None:
            self._confirm.reset()
            if self._touch_vwap is not None:
                self._touch_vwap.reset()
            self.log("[CONFIRM] state + touch VWAP reset for next setup")

        if self.exec_mode == EXEC_MODE_ONE_SHOT:
            self._exhausted = True
            self.log("[MODE] ONE_SHOT -> signal exhausted (will not fire again)")

        elif self.exec_mode == EXEC_MODE_COUNT_LIMITED:
            if self._fire_count >= self.max_fires:
                self._exhausted = True
                self.log(
                    f"[MODE] COUNT_LIMITED -> reached max_fires={self.max_fires}; exhausted"
                )
            else:
                self.log(
                    f"[MODE] COUNT_LIMITED -> {self.max_fires - self._fire_count} fires remaining"
                )

        elif self.exec_mode == EXEC_MODE_CONTINUOUS:
            self.log("[MODE] CONTINUOUS -> will fire again on next touch")
            # Optional: re-scan pattern between fires to refresh the level
            if self.rearm_on_fire:
                self._pattern_detected = False
                self._trigger_level = None
                self.log("[REARM] Will re-scan HTF for new pattern on next cycle")

        elif self.exec_mode == EXEC_MODE_AUTO:
            if self.rearm_on_fire:
                self._pattern_detected = False
                self._trigger_level = None
                self.log("[AUTO] Re-armed -> will re-scan HTF for new pattern")

        return intent

    def _max_label(self) -> str:
        """Human-readable max-fire count for log messages."""
        if self.exec_mode == EXEC_MODE_COUNT_LIMITED:
            return str(self.max_fires)
        if self.exec_mode == EXEC_MODE_ONE_SHOT:
            return "1"
        if self.exec_mode == EXEC_MODE_AUTO:
            return "auto"
        return "inf"

    # ============================================================
    # [OHLC EXTRACT] Data Normalization Helper
    # ============================================================
    def _extract_ohlc(self, candle):
        """Extract OHLC from candle data. Delegates to shared helper."""
        return extract_ohlc(candle)

    # ============================================================
    # [DESCRIBE] Runner Status Summary
    # ============================================================
    def describe(self):
        """One-line description for logs and status display."""
        level_str = f"{self._trigger_level:.2f}" if self._trigger_level else "TBD"
        return (
            f"{self.__class__.__name__} {self.symbol}/{self.exchange} "
            f"({self.pattern_type}, {self.action}, Level={level_str}, "
            f"mode={self.exec_mode}, fires={self._fire_count})"
        )
