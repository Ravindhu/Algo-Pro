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

        # Internal state
        self._fired = False                       # Legacy one-shot guard
        self._fire_count = 0                      # Total fires so far
        self._last_fire_ts: Optional[float] = None  # For cooldown
        self._start_ts = time.time()
        self._trigger_level: Optional[float] = None
        self._pattern_detected = False
        self._exhausted = False                   # All fires done

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

        self.log("=" * 60)
        self.log(
            f"FIRED #{self._fire_count}: {intent['action']} {fire_qty} shares -> {intent['reason']}"
        )
        self.log("=" * 60)

        # ---- Post-fire state management (per user-defined exec_mode) ----
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
