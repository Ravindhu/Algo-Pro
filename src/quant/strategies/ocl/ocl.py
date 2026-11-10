"""
Flow stage: Strategy (OCL keylevel, normal blueprint).
Status: PORTED; 13 smoke tests pass in sandbox (src/quant/tests/test_strategies_ocl.py)
PORTED COPY of Blue_engine/alpha/Statergy/ocl_keylevel.py (conformation entry model removed).
Tests: src/quant/tests/test_strategies_ocl.py

ocl_keylevel.py - OCL (Open-Close-Low) Keylevel Signal Strategy
===============================================================

OCL Strategy Logic:
-------------------
1. SCAN HTF (Higher Timeframe):
   - Fetches the last 3 candles (plus a 4th when enabled) from HTF
   - Detects Red-Red-Green (RRG) pattern ONLY (BUY setups)
   - NOTE: GRR (bearish / SELL) is intentionally NOT traded by this signal.
   ALL THREE BLUEPRINTS ARE BUY SETUPS (``setup_mode``):

   1) OCL - 2 levels, NO body filter:
        C3 closes above C2 open  -> aggressive = C2 open
        C4 closes above it instead -> aggressive = C3 close
        conservative = C3 open.

   2) SELL_FAILURE_TOP - 2 key levels, >50% bodies on C2 and C3:
        the failing pattern is a real SHORT: C2 GREEN (its open, e.g. 120,
        is the conservative SELL level) then C3 RED (its open, e.g. 135,
        is the aggressive SELL level). A later candle that CLOSES ABOVE
        the aggressive SELL level breaks the pattern; the levels then FLIP
        to BUY levels and the trade fires on the RETEST from above:
            aggressive BUY   = broken C3 open (e.g. 135) - tier 1
            conservative BUY = C2 open           (e.g. 120) - tier 2
        If tier 1 fails, tier 2 is the next entry.

   3) SELL_FAILURE_BOTTOM - 1 key level, >50% bodies:
        same failed-short trigger. The single BUY level is the OPEN of the
        LOWEST SWING candle in the window; the stop is that candle's LOW.
        Reached after BOTH SELL_FAILURE_TOP levels have failed (the monitor
        logs [ESCALATE] and switches the blueprint).

2. CALCULATE TOUCH TRIGGER LEVEL (user-selectable):
   - trigger_level_mode = CONSERVATIVE (default):
       Touch Level = Open of Candle3 (conservative_level_source=C3_OPEN)
       LEGACY: min(Low of C2, Low of C3)
   - trigger_level_mode = AGGRESSIVE:
       Touch Level = Close of Candle3 (aggressive_level_source=C3_CLOSE)
       LEGACY: Open of Candle2

3. MONITOR LTF (Lower Timeframe):
   - Polls LTF price at user-defined interval
   - Watches for price touch of trigger level
   - Fetches last 3 candles from user-defined LTF and detects the
     Red-Red-Green (RRG) pattern ONLY (BUY setups) on the LTF itself
   - Applies the Hilega-Milega gate on BOTH timeframes: bullish when
     RSI is above EMA(RSI). The oversold cap (RSI <= level) applies to
     the LTF always and to the HTF only if hm_htf_oversold_enabled
     (default False - the HTF gate is the plain cross, per operator).

4. TRIGGER POV ENGINE (User-Defined Execution):
   - Fires BUY only if ALL gates pass:
       HTF: RRG pattern + HM bullish (RSI > EMA; no oversold cap)
       LTF: touch + RRG pattern + HM bullish (RSI > EMA, RSI <= oversold)
   - Execution mode is user-defined (one_shot / continuous / count-limited)
   - Optional cooldown between fires
   - Optional per-fire slicing config (qty per fire, etc.)

5. RISK MANAGEMENT (IN-STRATEGY, not in the POV grid) - TWO-TIER STOPS:
   - HTF stop (pattern invalidation):
       SL_htf = min(low C2, low C3) of the HTF pattern. Fires when an HTF
       candle CLOSES below it (the HTF candle decides invalidation, not
       the LTF). Kills the whole setup; the scanner waits for a NEW HTF
       pattern. Wick-through does not count.
   - LTF stop (per-entry):
       SL_ltf = the entry's own stop. Fires when an LTF candle CLOSES
       below it while SL_htf is intact. The setup SURVIVES: the next
       entry level steps BELOW the hit LTF stop (cheaper re-entry),
       retried up to ``max_ltf_retries`` times per pattern.
   - After max_ltf_retries LTF stops: setup abandoned (pattern NOT
     invalidated - price is still above the HTF floor).
   - Target = entry + (entry - SL_ltf) * target_rr (fixed RR, 1:3
     default, optimizer-tunable ``target_rr``).
   - stop_loss/htf_stop_loss/target travel WITH the fire intent metadata
     so the risk manager and order manager can place protective orders.
   - Exit mode ``exit_mode`` (AUTO default) - decided by WHERE the entry
     fired:
       AUTO: entry at/near the AGGRESSIVE level (open of C2) -> FIXED,
             the hard RR target closes the trade (the ATR trail may
             still exit first).
             entry at the CONSERVATIVE level (deep min-low touch or a
             ladder re-entry) -> DYNAMIC, no hard target; the ATR
             trailing stop runs the trade.
       FIXED / DYNAMIC force one behaviour regardless of entry level.
   - Breakeven (all modes): once price reaches breakeven_rr x risk
     (default 1.5R) the stop is raised to entry +
     round_trip_cost_pct% (cost-neutral floor). After that point the
     DYNAMIC-VOLATILITY-SMOOTHING ATR may LIFT the stop further, and the
     live protective stop is always max(cost floor, DV-ATR lift).
   - ATR trailing stop (dynamic): after entry, stop = max(previous stop,
     highest close since entry - atr_multiplier * ATR(atr_period)); it
     only ratchets UP and exits when an LTF close falls below it. The
     fixed-RR target stays active in FIXED mode; whichever level is
     touched first closes the trade. atr_period / atr_multiplier are
     optimizer params.
   - DYNAMIC-VOLATILITY-SMOOTHING ATR (DV-ATR): a self-scaling trail.
     With realized volatility above its typical level the trail WIDENS
     (multiplier scaled up by ``dvs_sensitivity``), below it the trail
     TIGHTENS. distance = atr_multiplier * ATR * (1 + sens*(vol/ref-1)),
     clamped to [1/dvs_max_scale, 1/dvs_max_scale] times atr_multiplier.
     vol = stdev(TR)/mean(TR) over ``atr_period``, ref = the mean of that
     ratio over ``dvs_reference_period`` bars (causal). The classic
     stop = entry - risk * stop_fallback_atr_mult maintains TRADITIONAL
     STOP LOSS as a fallback floor and always stays below the DV-ATR
     trail, so the DV-ATR can take over only after it has lifted above
     it. The live protective stop = max(traditional SL, DV-ATR), and
     DV-ATR activation degree for sizing is ``dvs_atr_sizing_factor``.
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, Dict, Optional

from quant.core.session_clock import ist_now

from quant.indicators.pattern import (
    extract_ohlc,
    validate_pattern,
)
from abc import ABC, abstractmethod

# Confirmation-layer math. Imported at module level (not inside the gate) so
# an ImportError surfaces at import time, not halfway through a live run.
from quant.indicators.roc import find_flip, zscored_roc
from quant.indicators.indicators import (
    atr,
    ema,
    hilega_milega_rsi,
)


# Execution mode constants (user-selectable per run)
EXEC_MODE_ONE_SHOT = "ONE_SHOT"          # Fire once, then stop
EXEC_MODE_CONTINUOUS = "CONTINUOUS"      # Fire on every touch (with cooldown)
EXEC_MODE_COUNT_LIMITED = "COUNT_LIMITED"  # Fire up to N times (max_fires)
EXEC_MODE_AUTO = "AUTO"                  # One-shot per pattern detection

# Setup modes - the three tradeable blueprints. ALL ARE BUY SETUPS and each
# one is backtested, optimized and logged SEPARATELY (the label lands in
# every log line and in the fire intent metadata as ``setup_mode``).
SETUP_MODE_NORMAL = "OCL"                 # 2 levels, NO body filter
SETUP_MODE_SELL_FAILURE_TOP = "SELL_FAILURE_TOP"        # 2 key levels,
#    >50% bodies: broken C3 open -> aggressive BUY, C2 open -> conservative
SETUP_MODE_SELL_FAILURE_BOTTOM = "SELL_FAILURE_BOTTOM"  # 1 key level:
#    lowest swing candle open -> the single BUY level
SETUP_MODES = (SETUP_MODE_NORMAL, SETUP_MODE_SELL_FAILURE_TOP,
               SETUP_MODE_SELL_FAILURE_BOTTOM)


VALID_INTERVALS = frozenset({
    "1s", "5s", "10s", "15s", "30s", "45s",
    "1m", "2m", "3m", "5m", "10m", "15m", "20m", "30m",
    "1h", "2h", "3h", "4h",
    "D", "W", "M", "Q", "Y",
})

#: Legacy/verbose spellings seen in older configs and the docs. AUDIT
#: (2026-10-29, LIVE-12): ``master.json`` shipped ``"15minute"``/``"5minute"``,
#: which the API rejects with 400, so the OCL strategy could never load a
#: candle and simply waited forever.
_INTERVAL_ALIASES = {
    "1minute": "1m", "2minute": "2m", "3minute": "3m", "5minute": "5m",
    "10minute": "10m", "15minute": "15m", "20minute": "20m",
    "30minute": "30m", "60minute": "1h", "1hour": "1h",
    "2hour": "2h", "3hour": "3h", "4hour": "4h",
    "1day": "D", "day": "D", "daily": "D", "1d": "D",
    "1week": "W", "week": "W", "weekly": "W", "1w": "W",
    "1month": "M", "month": "M", "monthly": "M", "1mo": "M",
    "quarter": "Q", "yearly": "Y", "1year": "Y",
}


def normalize_interval(value: object) -> str:
    """Map a config interval to the API's vocabulary.

    Valid values pass through unchanged; known legacy spellings are translated;
    anything else is returned as-is so the API's own error names it, rather
    than silently substituting a different timeframe (which would change the
    strategy's behaviour without the operator knowing).
    """
    if value is None:
        return "5m"
    text = str(value).strip()
    if text in VALID_INTERVALS:
        return text
    return _INTERVAL_ALIASES.get(text.lower(), text)


class BaseSignal(ABC):
    """Abstract base for a signal strategy.

    Subclasses must implement detect(). They may also override log_state() to
    print human-readable status. The runner calls detect() repeatedly; when
    it returns a non-None intent, the runner hands the intent to a slicer.
    """

    def __init__(self, client: Any, config: Dict[str, Any]):
        """Store the SDK client and the parsed JSON config.

        Args:
            client: An initialized openalgo.api instance.
            config: Parsed JSON dictionary from blue_engine/input/*.json.
        """
        self.client = client
        self.config = config

    @abstractmethod
    def detect(self) -> Optional[Dict[str, Any]]:
        """Return a trade intent, or None to skip this cycle.

        Returns:
            A dict with at least:
                action:   "BUY" or "SELL"
                quantity: positive integer (shares/contracts/lots)
            Additional keys (e.g., "tag", "reason") are passed through to
            the slicer as metadata.
            Or None when no signal is present.
        """
        raise NotImplementedError

    def describe(self) -> str:
        """One-line human description, used in logs."""
        return f"{self.__class__.__name__} for {self.config.get('symbol', '?')}/{self.config.get('exchange', '?')}"

    def log(self, message: str) -> None:
        """Print a prefixed log line. Slicers and runner use the same prefix."""
        print(f"[signal:{self.__class__.__name__}] {message}")


class OclSignal(BaseSignal):
    """OCL (Open-Close-Low) Keylevel Signal - Pattern-based POV trigger.

    The class-level ``name`` is the registry key the optimizer dropdown
    surfaces (and the live runner references from the JSON config). It
    is set explicitly to ``ocl_keylevel`` so the registration does not
    collide with other "Keylevel" strategies and matches the
    convention ``<family>_<name>`` used across the alpha engine.

    Execution Behavior (User-Defined per run):
    -------------------------------------------
    - exec_mode = ONE_SHOT (default):
        Fires ONCE per run when the trigger is first touched, then stops.
    -------------------------------------------
    - exec_mode = ONE_SHOT (default):
        Fires ONCE per run when the trigger is first touched, then stops.
    - exec_mode = CONTINUOUS:
        Fires EVERY TIME the trigger is touched (with cooldown between fires).
    - exec_mode = COUNT_LIMITED:
        Fires up to max_fires times per run, then stops.
    - exec_mode = AUTO:
        Fires once per NEW pattern detection (re-arms when a fresh RRG forms).

    The class-level ``name`` is the registry key the optimizer dropdown
    surfaces (and the live runner references from the JSON config). It
    is set explicitly to ``ocl_keylevel`` so the registration does not
    collide with other "Keylevel" strategies and matches the
    convention ``<family>_<name>`` used across the alpha engine.
    """

    # Registry key for the optimizer dropdown and the live runner.
    # Set explicitly so a future "EngulfingKeylevelSignal" or
    # similar can coexist without a name collision.
    name = "ocl"
    fixed_setup_mode = "OCL"

    # Tunable parameter space, read by the AI Insight header in the
    # Optimizer dashboard and by the ``Blue_engine/optimizer/spaces.py``
    # built-in space. Each entry has:
    #   - kind:     "int" | "float" | "cat"
    #   - default:  the value the strategy uses when the JSON config
    #               does not override it
    #   - low/high: numeric bounds (int / float only)
    #   - choices:  list of legal values (cat only)
    #   - note:     one-line human description for the header table
    params_space = {
        # AUDIT (2026-10-29, LIVE-12): choices must be API-valid tokens
        # ("5m", "15m", "1h"); "15minute" is rejected with HTTP 400.
        "htf_interval":     {"kind": "cat",   "default": "15m",
                              "choices": ["5m", "15m", "1h"],
                              "note": "higher timeframe to scan"},
        "htf_candles":      {"kind": "int",   "default": 5,
                              "low": 3, "high": 10,
                              "note": "bars to scan on the HTF"},
        "pattern_type":     {"kind": "cat",   "default": "RRG",
                              "choices": ["RRG"],
                              "note": "RRG only (bullish BUY); GRR removed"},
        "trigger_level_mode": {"kind": "cat",  "default": "CONSERVATIVE",
                              "choices": ["CONSERVATIVE", "AGGRESSIVE"],
                              "note": "trigger: min-low (deep) vs open of 2nd red"},
        "trade_direction":  {"kind": "cat",   "default": "AUTO",
                              "choices": ["AUTO", "BUY", "SELL"],
                              "note": "BUY/SELL/AUTO from pattern"},
        "ltf_interval":     {"kind": "cat",   "default": "5m",
                              "choices": ["1m", "5m", "15m"],
                              "note": "lower timeframe to monitor"},
        "ltf_touch_buffer": {"kind": "float", "default": 0.05,
                              "low": 0.0, "high": 0.50,
                              "note": "price tolerance around the trigger"},
        "poll_seconds":     {"kind": "int",   "default": 10,
                              "low": 1, "high": 60,
                              "note": "poll cadence on the LTF"},
        "exec_mode":        {"kind": "cat",   "default": "ONE_SHOT",
                              "choices": ["ONE_SHOT", "CONTINUOUS",
                                          "COUNT_LIMITED", "AUTO"],
                              "note": "how many times the signal may fire"},
        "qty_per_fire":     {"kind": "int",   "default": 0,
                              "low": 1, "high": 10000,
                              "note": "quantity per fire (0 = total)"},
        "cooldown_seconds": {"kind": "float", "default": 0,
                              "low": 0.0, "high": 3600.0,
                              "note": "min gap between fires"},
        "max_fires":        {"kind": "int",   "default": 1,
                              "low": 1, "high": 100,
                              "note": "max fires (COUNT_LIMITED mode)"},
        # [LTF GATES] LTF pattern scan + Hilega-Milega indicator gate.
        # Both are REQUIRED for a fire: touch alone is no longer enough.
        "ltf_pattern_enabled": {"kind": "cat", "default": True,
                              "choices": [True, False],
                              "note": "require RRG on the last 3 LTF candles"},
        "hm_enabled":       {"kind": "cat",   "default": True,
                              "choices": [True, False],
                              "note": "HM bullish gate on HTF + LTF (RSI>EMA)"},
        "hm_rsi_period":    {"kind": "int",   "default": 9,
                              "low": 2, "high": 30,
                              "note": "HM: RSI period (NK default 9)"},
        "hm_ema_period":    {"kind": "int",   "default": 3,
                              "low": 1, "high": 20,
                              "note": "HM: EMA over RSI (NK default 3)"},
        "hm_oversold_level": {"kind": "float", "default": 30.0,
                              "low": 0.0, "high": 100.0,
                              "note": "HM: LTF buy only when RSI at/below this"},
        "hm_htf_oversold_enabled": {"kind": "cat", "default": False,
                              "choices": [True, False],
                              "note": "apply oversold cap to HTF too (opt-in)"},
        # [RISK MANAGEMENT] Structural stop + fixed RR target (in-strategy).
        # SL sits BELOW the pattern's structural low; it fires when an LTF
        # candle CLOSES below it (not just a wick). Target = SL distance
        # x target_rr from entry (1:3 by default).
        "target_rr":        {"kind": "float", "default": 3.0,
                              "low": 0.5, "high": 10.0,
                              "note": "risk-reward target (1:target_rr)"},
        # [EXIT MODE] AUTO (default): exit style chosen by WHERE the entry
        # fired - aggressive-level entry -> FIXED (hard RR target);
        # conservative-level entry (deep touch / ladder re-entry) ->
        # DYNAMIC (ATR trail only). FIXED/DYNAMIC force one behaviour.
        "exit_mode":        {"kind": "cat",   "default": "AUTO",
                              "choices": ["AUTO", "FIXED", "DYNAMIC"],
                              "note": "AUTO: aggressive entry->FIXED, conservative->ATR trail"},
        # [ATR TRAILING STOP] Dynamic trailing exit (optimizer-tunable).
        # After entry the stop trails at (highest close since entry -
        # atr_multiplier * ATR(atr_period)), ratcheting UP only. The
        # fixed-RR target stays active; whichever exits first wins.
        "atr_trail_enabled": {"kind": "cat", "default": True,
                              "choices": [True, False],
                              "note": "dynamic ATR trailing stop after entry"},
        "atr_period":       {"kind": "int",   "default": 14,
                              "low": 5, "high": 50,
                              "note": "ATR lookback for the trailing stop"},
        "atr_multiplier":   {"kind": "float", "default": 2.0,
                              "low": 0.5, "high": 6.0,
                              "note": "trail distance in ATR multiples"},
        # [DYNAMIC-VOLATILITY-SMOOTHING ATR] Self-scaling trail. The ATR
        # distance is multiplied by (1 + sensitivity*(vol/ref-1)) and
        # clamped, so the stop widens in high volatility and tightens in
        # low volatility. The traditional stop-loss stays as the fallback
        # floor (stop_fallback_atr_mult) and always sits BELOW the DV-ATR.
        "dvs_enabled":      {"kind": "cat", "default": True,
                              "choices": [True, False],
                              "note": "dynamic volatility smoothing ATR trail"},
        "dvs_sensitivity":  {"kind": "float", "default": 1.0,
                              "low": 0.0, "high": 3.0,
                              "note": "how strongly vol change scales the trail"},
        "dvs_max_scale":    {"kind": "float", "default": 2.5,
                              "low": 1.0, "high": 5.0,
                              "note": "max widening/tightening factor of the ATR distance"},
        "dvs_reference_period": {"kind": "int", "default": 50,
                              "low": 10, "high": 300,
                              "note": "lookback for the reference (typical) volatility"},
        "dvs_atr_sizing_factor": {"kind": "float", "default": 1.0,
                              "low": 0.1, "high": 2.0,
                              "note": "position-size multiplier when DV-ATR owns the stop"},
        "stop_fallback_atr_mult": {"kind": "float", "default": 2.0,
                              "low": 0.2, "high": 10.0,
                              "note": "traditional SL = entry - mult*ATR (fallback, below DV-ATR)"},
        # [SETUP RULES] C3 must CLOSE above C2's open. If C3 fails, C4 may
        # close above it instead (setup confirmed one candle later).
        "require_close_above_c2_open": {"kind": "cat", "default": True,
                              "choices": [True, False],
                              "note": "C3 close > C2 open, else C4 close > C2 open"},
        "allow_c4_confirmation": {"kind": "cat", "default": True,
                              "choices": [True, False],
                              "note": "accept a 4th candle closing above C2 open"},
        # [LEVELS] AGGRESSIVE = close of C3, CONSERVATIVE = open of C3.
        "aggressive_level_source": {"kind": "cat", "default": "C3_CLOSE",
                              "choices": ["C3_CLOSE", "LEGACY"],
                              "note": "C3_CLOSE = close of 3rd candle; LEGACY = old open-of-C2"},
        "conservative_level_source": {"kind": "cat", "default": "C3_OPEN",
                              "choices": ["C3_OPEN", "LEGACY"],
                              "note": "C3_OPEN = open of 3rd candle; LEGACY = min low C2/C3"},
        "min_body_pct_c23": {"kind": "float", "default": 0.5,
                              "low": 0.0, "high": 1.0,
                              "note": "C2 and C3 bodies must exceed this body/range ratio"},
        # [BUY-ON-SELL-FAILURE] C4 closes above the conservative sell level
        # (C3 open) -> levels FLIP and the retest from above is the entry.
        "sell_failure_enabled": {"kind": "cat", "default": False,
                              "choices": [True, False],
                              "note": "buy the failed sell: C4 closes above C3 open, entry on retest"},
        # [SETUP MODE] The three blueprints, each optimized/backtested/logged
        # separately: OCL (classic buy), SELL_FAILURE_BOTTOM (buy the failed
        # short on a retest from above), SELL_FAILURE_TOP (sell the failed
        # long on a retest from below).
        "setup_mode":       {"kind": "cat",   "default": "OCL",
                              "choices": ["OCL", "SELL_FAILURE_TOP",
                                          "SELL_FAILURE_BOTTOM"],
                              "note": "which BUY blueprint (run each separately)"},
        "apply_body_filter_failure": {"kind": "cat", "default": True,
                              "choices": [True, False],
                              "note": ">50% body filter on failing modes (TOP=off, BOTTOM=on)"},
    }

    def __init__(self, client: Any, config: Dict[str, Any]):
        super().__init__(client, config)

        # [HTF SETTINGS] Higher Timeframe Configuration
        # AUDIT (2026-10-29, LIVE-12): the API accepts "15m", not "15minute";
        # an un-normalised value made every history() call fail with HTTP 400
        # and the signal simply waited forever. Normalised at this boundary.
        self.htf_interval = normalize_interval(config.get("htf_interval", "15m"))
        self.htf_candles = int(config.get("htf_candles", 5))

        # [PATTERN SETTINGS] RRG-only pattern. min_body_pct concept is
        # REMOVED (operator decision): any Red-Red-Green shape qualifies
        # (validate_pattern enforces the candle colors only).
        self.pattern_type = config.get("pattern_type", "RRG").upper()
        if self.pattern_type != "RRG":
            raise ValueError(
                f"pattern_type must be 'RRG' (BUY only); GRR removed. Got {self.pattern_type!r}"
            )
        self.trade_direction = config.get("trade_direction", "AUTO").upper()
        if self.trade_direction == "SELL":
            raise ValueError(
                "trade_direction='SELL' is not supported: OCL trades RRG (BUY) only"
            )

        # [TRIGGER LEVEL MODE] CONSERVATIVE vs AGGRESSIVE selection.
        # CONSERVATIVE: min(Low of C2, Low of C3)  - deeper level, waits more
        # AGGRESSIVE:   Open of C2 (second red candle) - nearer level
        self.trigger_level_mode = str(
            config.get("trigger_level_mode", "CONSERVATIVE")
        ).upper().strip()
        if self.trigger_level_mode not in ("CONSERVATIVE", "AGGRESSIVE"):
            raise ValueError(
                "trigger_level_mode must be CONSERVATIVE or AGGRESSIVE; "
                f"got {self.trigger_level_mode!r}"
            )

        # [SETUP RULES] C3 close above C2 open (C4 fallback); body filter
        # on C2/C3; new level sources; buy-on-sell-failure mode.
        self.require_close_above_c2_open = bool(
            config.get("require_close_above_c2_open", True))
        self.allow_c4_confirmation = bool(
            config.get("allow_c4_confirmation", True))
        self.aggressive_level_source = str(
            config.get("aggressive_level_source", "C3_CLOSE")).upper().strip()
        if self.aggressive_level_source not in ("C3_CLOSE", "LEGACY"):
            raise ValueError(
                "aggressive_level_source must be C3_CLOSE or LEGACY; "
                f"got {self.aggressive_level_source!r}")
        self.conservative_level_source = str(
            config.get("conservative_level_source", "C3_OPEN")).upper().strip()
        if self.conservative_level_source not in ("C3_OPEN", "LEGACY"):
            raise ValueError(
                "conservative_level_source must be C3_OPEN or LEGACY; "
                f"got {self.conservative_level_source!r}")
        self.min_body_pct_c23 = float(config.get("min_body_pct_c23", 0.5))
        if not 0.0 <= self.min_body_pct_c23 <= 1.0:
            raise ValueError("min_body_pct_c23 must be within [0, 1]")
        self.sell_failure_enabled = bool(
            config.get("sell_failure_enabled", False))
        self.setup_mode = str(
            config.get("setup_mode", "OCL")).upper().strip()
        if self.setup_mode == "SELL_FAILURE_LOW":
            # Retired alias: the single-level blueprint is now BOTTOM.
            self.setup_mode = SETUP_MODE_SELL_FAILURE_BOTTOM
        if self.setup_mode not in SETUP_MODES:
            raise ValueError(
                f"setup_mode must be one of {SETUP_MODES}; "
                f"got {self.setup_mode!r}")
        # sell_failure_enabled is the legacy 2-way switch; keep it as a
        # shim so old configs map onto the BOTTOM blueprint.
        if self.sell_failure_enabled and self.setup_mode == SETUP_MODE_NORMAL:
            self.setup_mode = SETUP_MODE_SELL_FAILURE_BOTTOM
        if self.setup_mode != self.fixed_setup_mode:
            raise ValueError(f"OclSignal runs only setup_mode={self.fixed_setup_mode!r}; got {self.setup_mode!r}")
        # Body filter per blueprint (operator: TOP has none, BOTTOM and the
        # other blueprints use the >50% rule). Explicit config wins.
        default_body = (SETUP_MODE_SELL_FAILURE_TOP != self.setup_mode)
        self.apply_body_filter_failure = bool(
            config.get("apply_body_filter_failure", default_body))
        # State: C4 confirmation candle, sell-failure bookkeeping.
        self._setup_candle: Optional[dict] = None   # C3 or C4 confirmation
        self._c2_open_ref: Optional[float] = None   # C2 open of the sequence
        self._core_window: list = []                # last raw 4-candle window
        self._core_positions: list = []             # indices of the core
        self._is_float_mode = False                 # inside _is_ocl_sequence
        self._core_end: Optional[dict] = None       # last candle of the core
        self._sell_failure_armed = False            # failure blueprint armed
        self._sell_failure_broken = False           # level broken, awaiting retest
        self._broken_sell_level: Optional[float] = None
        self._low_swing_level: Optional[float] = None
        # SELL_FAILURE_TOP two-tier ladder bookkeeping.
        self._oc_top_used = False       # aggressive level already failed
        self._oc_bottom_used = False    # already escalated to BOTTOM

        # [LTF SETTINGS] Lower Timeframe Monitoring
        _norm_iv = normalize_interval
        self.ltf_interval = _norm_iv(config.get("ltf_interval", "5m"))
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

        # [LTF GATES] LTF RRG scan + Hilega-Milega indicator gate.
        # Fire condition = touch AND ltf_pattern AND HM bullish (when on).
        self.ltf_pattern_enabled = bool(config.get("ltf_pattern_enabled", True))
        self.hm_enabled = bool(config.get("hm_enabled", True))
        self.hm_rsi_period = int(config.get("hm_rsi_period", 9))
        self.hm_ema_period = int(config.get("hm_ema_period", 3))
        self.hm_oversold_level = float(config.get("hm_oversold_level", 30.0))
        # Oversold cap applies to the LTF always; on the HTF it is OPT-IN
        # (default OFF): a monthly RSI<=cap requirement fires only in
        # distressed tape, so the HTF gate is the plain RSI>EMA cross.
        self.hm_htf_oversold_enabled = bool(
            config.get("hm_htf_oversold_enabled", False)
        )
        if self.hm_rsi_period < 2:
            raise ValueError("hm_rsi_period must be >= 2")
        if self.hm_ema_period < 1:
            raise ValueError("hm_ema_period must be >= 1")
        if not (0 <= self.hm_oversold_level <= 100):
            raise ValueError("hm_oversold_level must be in [0, 100]")

        # [RISK MANAGEMENT] Structural stop-loss + fixed RR target.
        # SL = min(low C2, low C3) of the HTF pattern (ALWAYS the structural
        # low, regardless of trigger_level_mode: the pattern is invalid if
        # price closes below where it based). Trigger rule: an LTF candle
        # must CLOSE below the SL - a wick through does not stop out.
        # Target = entry + (entry - SL) * target_rr (1:3 default).
        self.target_rr = float(config.get("target_rr", 3.0))
        if self.target_rr <= 0:
            raise ValueError("target_rr must be > 0")
        # [EXIT MODE] AUTO decides by entry level: an entry taken at the
        # AGGRESSIVE level exits on the hard RR target (FIXED); an entry
        # taken at the CONSERVATIVE (deeper) level lets the ATR trailing
        # stop run the trade (DYNAMIC). FIXED/DYNAMIC force one mode.
        self.exit_mode = str(config.get("exit_mode", "AUTO")).upper().strip()
        if self.exit_mode not in ("AUTO", "FIXED", "DYNAMIC"):
            raise ValueError(
                f"exit_mode must be AUTO, FIXED or DYNAMIC; got {self.exit_mode!r}"
            )
        # Both trigger levels are tracked so AUTO can tell WHERE the
        # entry happened. The effective exit mode is resolved at fire time.
        self._conservative_level: Optional[float] = None
        self._aggressive_level: Optional[float] = None
        self._entry_exit_mode: Optional[str] = None
        # [BREAKEVEN] When price reaches breakeven_rr x risk, the stop is
        # ratcheted to entry + round_trip_cost_pct (% of entry) so the
        # worst outcome after breakeven is scratch-free.
        self.breakeven_rr = float(config.get("breakeven_rr", 1.5))
        self.round_trip_cost_pct = float(
            config.get("round_trip_cost_pct", 0.20)
        )
        if self.breakeven_rr <= 0:
            raise ValueError("breakeven_rr must be > 0")
        if self.round_trip_cost_pct < 0:
            raise ValueError("round_trip_cost_pct must be >= 0")
        self._breakeven_armed = False
        self.max_ltf_retries = int(config.get("max_ltf_retries", 3))
        if self.max_ltf_retries < 1:
            raise ValueError("max_ltf_retries must be >= 1")
        self._htf_stop_loss: Optional[float] = None   # pattern structural low
        self._ltf_stop_loss: Optional[float] = None   # current entry's SL
        self._target: Optional[float] = None

        # [ATR TRAILING STOP] Dynamic trailing exit (in-strategy). After a
        # fire, the running stop = max(ratchet, highest_close_since_entry
        # - atr_multiplier * ATR(atr_period)). Only ratchets UP for BUY.
        self.atr_trail_enabled = bool(config.get("atr_trail_enabled", True))
        self.atr_period = int(config.get("atr_period", 14))
        self.atr_multiplier = float(config.get("atr_multiplier", 2.0))
        if self.atr_period < 2:
            raise ValueError("atr_period must be >= 2")
        if self.atr_multiplier <= 0:
            raise ValueError("atr_multiplier must be > 0")
        # [DV-ATR] Self-scaling trail parameters.
        self.dvs_enabled = bool(config.get("dvs_enabled", True))
        self.dvs_sensitivity = float(config.get("dvs_sensitivity", 1.0))
        self.dvs_max_scale = float(config.get("dvs_max_scale", 2.5))
        self.dvs_reference_period = int(
            config.get("dvs_reference_period", 50))
        self.dvs_atr_sizing_factor = float(
            config.get("dvs_atr_sizing_factor", 1.0))
        self.stop_fallback_atr_mult = float(
            config.get("stop_fallback_atr_mult", 2.0))
        if self.dvs_sensitivity < 0:
            raise ValueError("dvs_sensitivity must be >= 0")
        if self.dvs_max_scale < 1.0:
            raise ValueError("dvs_max_scale must be >= 1.0")
        if self.dvs_reference_period < 5:
            raise ValueError("dvs_reference_period must be >= 5")
        if self.dvs_atr_sizing_factor <= 0:
            raise ValueError("dvs_atr_sizing_factor must be > 0")
        if self.stop_fallback_atr_mult <= 0:
            raise ValueError("stop_fallback_atr_mult must be > 0")
        self._trail_active = False
        self._highest_close: Optional[float] = None
        self._trail_stop: Optional[float] = None
        self._dvs_scale: float = 1.0        # last applied volatility scale
        self._dvs_stop: Optional[float] = None   # DV-ATR trail level
        self._dvs_active = False
        self._traditional_sl: Optional[float] = None  # fallback floor
        self._traditional_sl_frozen = False

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
        self._ltf_retry_count = 0                 # LTF SL hits this setup
        self._last_fire_ts: Optional[float] = None  # For cooldown
        self._start_ts = time.time()
        self._trigger_level: Optional[float] = None
        self._pattern_detected = False
        self._exhausted = False                   # All fires done
        self._position_open = False
        self._position_quantity = 0
        self._entry_price: Optional[float] = None
        self._position_stop: Optional[float] = None
        self._position_target: Optional[float] = None
        # Confirmation-layer state (only used when confirm_enabled)
        if self.confirm_enabled:
            from quant.strategies.signals.confirm_state import ConfirmMachine
            from quant.indicators.vwap_band import BuyerInitiatedDailyVWAP

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
            f"Pattern={self.pattern_type}, Direction={self.trade_direction}, "
            f"Trigger={self.trigger_level_mode}"
        )
        self.log(
            f"Execution: mode={self.exec_mode}, total_qty={self.total_quantity}, "
            f"qty_per_fire={self.qty_per_fire}, max_fires={self.max_fires}, "
            f"cooldown={self.cooldown_seconds}s, rearm={self.rearm_on_fire}"
        )
        self.log(
            f"LTF gates: ltf_pattern={self.ltf_pattern_enabled}, "
            f"HM={self.hm_enabled} on HTF+LTF "
            f"(RSI={self.hm_rsi_period}, EMA={self.hm_ema_period}, "
            f"oversold<={self.hm_oversold_level}, "
            f"htf_cap={self.hm_htf_oversold_enabled})"
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
        # -- 1. Exhaustion guard applies only when no position needs an exit.
        if self._exhausted and not self._position_open:
            return None

        # -- 2. Cooldown only delays entry fires, never protective exits.
        if (not self._position_open and self._last_fire_ts is not None
                and self.cooldown_seconds > 0):
            elapsed = time.time() - self._last_fire_ts
            if elapsed < self.cooldown_seconds:
                remaining = self.cooldown_seconds - elapsed
                self.log(
                    f"[COOLDOWN] {remaining:.1f}s remaining before next fire"
                )
                time.sleep(min(remaining, self.poll_seconds))
                return None

        # -- 3. Overall timeout --
        if not self._position_open and self.wait_seconds is not None:
            elapsed = time.time() - self._start_ts
            if elapsed > float(self.wait_seconds):
                self.log(f"[TIMEOUT] {self.wait_seconds}s elapsed; giving up")
                self._exhausted = True
                return None

        # Keep a fired position under strategy-level supervision until an
        # explicit close intent is returned (HTF invalidation, LTF stop,
        # fixed target, or ATR trail).
        if self._position_open:
            return self._monitor_open_position()

        # One-shot applies to new entries only. Do not let it block
        # supervision of an already-open position or its exit intent.
        if self.one_shot and self._fired and self.exec_mode == EXEC_MODE_ONE_SHOT:
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
            need = 4 if self.allow_c4_confirmation else 3
            if df is None or len(df) < need:
                self.log(f"[HTF ERROR] Insufficient candles ({len(df) if df else 0})")
                return False

            # Skip the still-forming candle when history carries it: every
            # rule below is a CLOSE rule, so an open bar would fake a close.
            if self._last_bar_is_forming(response):
                df = df.iloc[:-1]
                if len(df) < need:
                    self.log(f"[HTF ERROR] Insufficient closed candles ({len(df)})")
                    return False

            # The RRG core is always the three candles BEFORE the last one
            # when a 4th candle is available for confirmation; otherwise the
            # last three candles are the core.
            n = len(df)
            if self.allow_c4_confirmation and n >= 4:
                c1 = self._extract_ohlc(df.iloc[-4])
                c2 = self._extract_ohlc(df.iloc[-3])
                c3 = self._extract_ohlc(df.iloc[-2])
                c4 = self._extract_ohlc(df.iloc[-1])
            else:
                c1 = self._extract_ohlc(df.iloc[-3])
                c2 = self._extract_ohlc(df.iloc[-2])
                c3 = self._extract_ohlc(df.iloc[-1])
                c4 = None
            # The window is re-selected below (_select_core); keep the raw
            # rows so the shifted (C2..C4) case can be logged truthfully.
            _raw_window = [c1, c2, c3, c4]

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
            if c4 is not None:
                self.log(
                    f"[HTF DATA] C4: O={c4['open']:.2f} H={c4['high']:.2f} "
                    f"L={c4['low']:.2f} C={c4['close']:.2f}"
                )

            # COMMON GATE (all three blueprints): the original sequence must
            # satisfy the OCL criteria. Per operator blueprint that is
            #   C1 RED, C2 RED, C3 GREEN, >50% bodies on C2/C3,
            #   and C3 closing above C2's open.
            # The failure blueprints apply the SAME gate inside C4 when the
            # core sits one candle later (see _resolve_setup_mode).
            self._core_window = _raw_window
            core = self._select_core(df, c2, c3, c4)
            if core is None:
                self.log(
                    f"[SETUP REJECT] no OCL sequence (C1 red, C2 red, C3 green "
                    f"and C3 close > C2 open) in the last 4 candles")
                return False
            if core[:3] != _raw_window[:3]:
                self.log(
                    "[SETUP] core window shifted: sequence is C2..C4 "
                    f"(O={core[0]['open']:.2f}, C={core[0]['close']:.2f} -> "
                    f"C={(core[2]['close'] if len(core) > 2 else core[-1]['close']):.2f})")
            c1, c2, c3 = core[0], core[1], core[2]
            self._core_end = c3
            # Confirmation / failure candle = newest closed candle AFTER the
            # core. Identity (not equality) matters: two candles can carry
            # identical OHLC and the position is what defines the window.
            # Confirmation / failure candle(s) = closed candles AFTER the
            # core in the raw window, newest first.
            _core_win = self._core_window
            _pos = (self._core_positions[2] if len(self._core_positions) > 2
                    else len(_core_win) - 1)
            _after = [w for w in _core_win[_pos + 1:] if w is not None]
            c4 = _after[-1] if _after else None
            self._c2_open_ref = float(c2["open"])
            self.log(
                f"[CORE] {self.setup_mode}: core=C{self._core_positions} "
                f"C2_open={c2['open']:.2f} C3_open={c3['open']:.2f} "
                f"C3_close={c3['close']:.2f} "
                f"C4={'%.2f' % c4['close'] if c4 else 'none'}")

            # HM gate on the HTF as well: same simplified rule (RSI > EMA).
            if self.hm_enabled and not self._hilega_milega_bullish(df, label="HTF"):
                self.log("[HTF HM GATE] Hilega-Milega not bullish on HTF; waiting")
                return False

            # ---- Confirmation + levels per SETUP MODE ----
            mode = self._resolve_setup_mode(c1, c2, c3, c4)
            if mode is None:
                return False
            self.setup_mode = mode
            if mode == SETUP_MODE_NORMAL:
                # ---- OCL (2 levels, NO body filter) ----
                # C3 closes above C2's open -> AGGRESSIVE = C2 open.
                # C4 closes above it instead  -> AGGRESSIVE = C3 close.
                # CONSERVATIVE is always C3's open.
                confirmed, confirm_candle, _sf = self._resolve_confirmation(
                    c2, c3, c4)
                if not confirmed:
                    return False
                self._setup_candle = confirm_candle
                self._sell_failure_broken = False
                self._oc_top_used = False
                c3_break = float(c3["close"]) > float(c2["open"])
                aggr = (float(c2["open"]) if c3_break
                        else float(c3["close"]))
                cons = float(c3["open"])
                self._aggressive_level = aggr
                self._conservative_level = cons
                self._sell_failure_armed = False
                self._broken_sell_level = None
                self.log(
                    f"[OCL] {'C3' if c3_break else 'C4'} broke "
                    f"{'C2 open' if c3_break else 'via C3 close'} -> "
                    f"aggressive {aggr:.2f}, conservative {cons:.2f}")
                self._trigger_level = self._calculate_trigger_level(
                    c1, c2, c3)
                base_candles = [c2, c3, confirm_candle]
                base_side = "BUY"
            else:
                # ---- SELL-FAILURE family ----
                # The failing pattern is a REAL SHORT: C2 GREEN (open = the
                # conservative sell level), C3 RED (open = the aggressive sell
                # level). A later candle that CLOSES ABOVE the aggressive sell
                # level breaks it; any such candle turns the pattern into the
                # sell-FAIL setup and the levels FLIP to buy levels on the
                # retest from above.
                fail_candle = self._sell_failure_candle(c2, c3, c4)
                if fail_candle is None:
                    return False
                # Both SELL levels must be FRESH: no candle between C3 and the
                # failure candle may have already traded at or above them.
                if not self._levels_are_fresh(
                        self._core_window, self._core_positions, c2, c3,
                        fail_candle):
                    self.log(
                        "[FRESH] a candle before the break already traded "
                        "at/above a sell level -> sequence stale, skip")
                    return False
                self._setup_candle = fail_candle
                self._sell_failure_broken = False
                self._oc_top_used = False
                aggr = float(c3["open"])      # broken aggressive SELL level
                cons = float(c2["open"])      # conservative SELL level
                self._aggressive_level = aggr
                self._conservative_level = cons
                self._sell_failure_armed = True
                self._broken_sell_level = aggr
                if mode == SETUP_MODE_SELL_FAILURE_TOP:
                    # 2 levels -> the retest ladder starts at the nearer
                    # (aggressive) level.
                    self._trigger_level = aggr
                    self.log(
                        f"[SELL_FAILURE_TOP] broken C3 open {aggr:.2f} = "
                        f"aggressive BUY level, C1 open {cons:.2f} = "
                        f"conservative BUY level")
                else:  # SETUP_MODE_SELL_FAILURE_BOTTOM
                    # 1 level -> the LOWEST SWING candle's open.
                    low_candle = self._lowest_swing_candle(
                        [c1, c2, c3, c4, fail_candle])
                    if low_candle is None:
                        self.log("[SELL_FAILURE_BOTTOM] no swing candle; skip")
                        return False
                    self._low_swing_level = float(low_candle["open"])
                    self._conservative_level = self._low_swing_level
                    self._trigger_level = self._low_swing_level
                    self.log(
                        f"[SELL_FAILURE_BOTTOM] lowest swing open "
                        f"{self._low_swing_level:.2f} = SINGLE BUY level "
                        f"(broken C3 open {aggr:.2f} = sell failure trigger)")
                base_candles = [c2, c3, fail_candle]
                base_side = "BUY"

            # HTF structural stop-loss: beyond the pattern's base. BUY
            # stops below the base lows; SELL stops above the base highs.
            if base_side == "BUY":
                self._htf_stop_loss = min(
                    float(c["low"]) for c in base_candles)
            else:
                self._htf_stop_loss = max(
                    float(c["high"]) for c in base_candles)
            self._ltf_stop_loss = None
            self._ltf_retry_count = 0

            # Every blueprint in this family is a BUY setup. The direction is
            # fixed here (never by config) so no SELL intent can leak in.
            self.action = "BUY"

            self._pattern_detected = True
            self.log(
                f"[HTF PATTERN] OK {self.pattern_type}/{mode} detected! "
                f"Level={self._trigger_level:.2f}, "
                f"HTF-SL={self._htf_stop_loss:.2f}, Action={self.action}"
            )
            return True

        except Exception as e:
            self.log(f"[HTF ERROR] {e}")
            return False

    # ============================================================
    # [PATTERN VALIDATION] RRG Candlestick Validation
    # ============================================================
    def _validate_pattern(self, c1, c2, c3):
        """Validate the RRG (Red-Red-Green) pattern. Delegates to shared helper."""
        ok = validate_pattern(c1, c2, c3, self.pattern_type, 0.0)
        if ok:
            self.log("[PATTERN] OK Validated")
        else:
            self.log(f"[PATTERN MISMATCH] Expected {self.pattern_type}")
        return ok

    # ============================================================
    # [TRIGGER CALCULATION] Key Level (CONSERVATIVE / AGGRESSIVE)
    # ============================================================
    def _calculate_trigger_level(self, c1, c2, c3):
        """Compute the touch trigger level per trigger_level_mode.

        Level sources are configurable via ``conservative_level_source`` /
        ``aggressive_level_source``:

        CONSERVATIVE (default source C3_OPEN):
            OPEN of Candle3 - the conservative buy level.
            LEGACY source: min(Low C2, Low C3) - the deeper structural low.
        AGGRESSIVE (default source C3_CLOSE):
            CLOSE of Candle3 - the aggressive buy level, i.e. the price at
            which the third candle reclaimed C2's open.
            LEGACY source: Open of Candle2.
        """
        # Failure blueprints are resolved by _record_trigger_levels (broken
        # level, or the lowest swing open for the LOW blueprint). This branch
        # only guards a call that happens before that step.
        if self._sell_failure_armed:
            level = float(c3["open"])
            self.log(
                f"[TRIGGER] {self.setup_mode}: broken sell level -> {level:.2f}")
            return level
        # OCL blueprint (operator spec):
        #   C3 breaks C2 open  -> AGGRESSIVE level = C2 open
        #   C4 breaks instead  -> AGGRESSIVE level = C3 close
        # CONSERVATIVE is always C3 open (the deeper key level).
        c3_break = float(c3["close"]) > float(c2["open"])
        aggr = float(c2["open"]) if c3_break else float(c3["close"])
        cons = float(c3["open"])
        self._aggressive_level = aggr
        self._conservative_level = cons
        if self.trigger_level_mode == "AGGRESSIVE":
            level = aggr
            self.log(
                f"[TRIGGER] AGGRESSIVE (OCL): "
                f"{'C2 open' if c3_break else 'C3 close'} -> {level:.2f}")
        else:
            level = cons
            self.log(f"[TRIGGER] CONSERVATIVE (OCL): C3 open -> {level:.2f}")
        return level

    def _record_trigger_levels(self, c2, c3, confirm_candle=None,
                               sell_failure: bool = False,
                               broken: Optional[float] = None,
                               former: Optional[float] = None) -> None:
        """Capture BOTH levels (aggressive + conservative) for exit_mode
        AUTO, so the strategy knows whether the entry that eventually
        fires happened at the aggressive or the conservative level.

        Default mapping (operator blueprint):
            aggressive   = CLOSE of C3  (the candle that reclaimed C2 open)
            conservative = OPEN  of C3
        In BUY-ON-SELL-FAILURE the two roles FLIP: the broken conservative
        SELL level (C3 open) becomes the aggressive BUY level and the
        aggressive sell level (C3 close) becomes the conservative BUY level.
        """
        if self.aggressive_level_source == "C3_CLOSE":
            aggr = float(c3["close"])
        else:
            aggr = float(c2["open"])
        if self.conservative_level_source == "C3_OPEN":
            cons = float(c3["open"])
        else:
            cons = min(float(c2["low"]), float(c3["low"]))
        if sell_failure and confirm_candle is not None:
            # Roles FLIP: the broken CONSERVATIVE SELL level (C3 open)
            # becomes the AGGRESSIVE BUY level - the retest line where the
            # entry fires - and the former aggressive level (C3 close)
            # becomes the CONSERVATIVE BUY level.
            self._aggressive_level = (cons if broken is None else broken)
            self._conservative_level = (aggr if former is None else former)
        else:
            self._aggressive_level = aggr
            self._conservative_level = cons
        self._sell_failure_armed = bool(sell_failure)
        self._broken_sell_level = cons if sell_failure else None
        if sell_failure:
            # The retest level is the blueprint's entry level:
            #   TOP    -> the broken aggressive level (first tier)
            #   BOTTOM -> the lowest swing candle open (single level)
            self._trigger_level = self._retest_level()
        # Sanity: the aggressive level of a retest blueprint IS the broken
        # level, so the two must agree.
        if (sell_failure and self._broken_sell_level is not None
                and abs(self._aggressive_level - self._broken_sell_level)
                > 1e-9):
            self.log(
                f"[SETUP WARN] aggressive {self._aggressive_level:.2f} != "
                f"broken {self._broken_sell_level:.2f}")

    # ============================================================
    # [SETUP MODES] three blueprints, each run/optimized separately
    # ============================================================
    def _resolve_setup_mode(self, c1, c2, c3, c4):
        """Return the setup mode that the current HTF window satisfies.

        OCL (normal): C3 (else C4) closes above C2's open - bullish keylevel.
        SELL_FAILURE_BOTTOM: an OCL SHORT was set up (C3 close below its
            open) and a later candle CLOSES ABOVE the conservative sell
            level (C3 open) - the failed short is bought on the retest.
        SELL_FAILURE_TOP: an OCL LONG was set up (C3 close above its open)
            and a later candle CLOSES BELOW the conservative buy level
            (C3 close) - the failed long is sold on the retest.

        Returns None when the window does not satisfy the configured mode.
        """
        mode = self.setup_mode
        self.log(
            f"[MODE CHECK] {mode} | c2 open {c2['open']:.2f} c3 close "
            f"{c3['close']:.2f} | c4 "
            f"{('close %.2f' % c4['close']) if c4 else 'none'}")
        if mode == SETUP_MODE_NORMAL:
            return mode if self._normal_setup_ok(c2, c3, c4) else None
        if mode in (SETUP_MODE_SELL_FAILURE_BOTTOM, SETUP_MODE_SELL_FAILURE_TOP):
            # A candle must CLOSE ABOVE the aggressive SELL level (C3 open)
            # to break the pattern.
            if self._sell_failure_candle(c2, c3, c4) is None:
                return None
            return mode
        return None

    def _is_ocl_sequence(self, c1, c2, c3, min_body_pct: float) -> bool:
        """C1 red, C2 red, C3 green, C3 close > C2 open, >body C2/C3."""
        if not (c1["close"] < c1["open"] and c2["close"] < c2["open"]
                and c3["close"] > c3["open"]):
            return False
        if float(c3["close"]) <= float(c2["open"]):
            return False
        if min_body_pct > 0:
            for c in (c2, c3):
                rng = float(c["high"]) - float(c["low"])
                if rng <= 0:
                    return False
                if abs(float(c["close"]) - float(c["open"])) / rng \
                        <= min_body_pct:
                    return False
        return True

    def _select_core(self, df, c2, c3, c4):
        """Pick the OCL core window out of the last four candles.

        Preference: C1..C3 (the classic last-three window). When that does
        not satisfy the sequence but the shifted window C2..C4 does, the
        shifted window is used and its last candle becomes the confirmation
        candle. Returns [c1, c2, c3] or [c2, c3, c4] as OHLC dicts, or None.
        """
        n = len(df)
        body = self.min_body_pct_c23
        if n < 3:
            return None
        # Candle 1 only has to be RED: the blueprint sizes C2 and C3, and
        # ``body`` (captured above) is the threshold the window scan applies
        # to exactly those two candles. _select_core_windows is the ONLY
        # gate and it also records _core_positions, so the caller can find
        # the confirmation/failure candle by INDEX.
        return self._select_core_windows(df, n, body)

    def _select_core_windows(self, df, n, body):
        """Window scan behind :meth:`_select_core` (C2/C3 body only)."""
        # Windows come from the SAME raw objects stored in _core_window,
        # which is what makes position tracking reliable.
        raw = [w for w in (self._core_window or []) if w is not None]
        if len(raw) < 3:
            return None
        if len(raw) >= 4:
            w3 = raw[1:]
            w4 = raw
        else:
            w3 = raw
            w4 = None
        # Every returned window is exactly THREE candles: the OCL core.
        # A 4th row is never part of the core - it is the confirmation or
        # failure candle and is read from the caller's c4.

        failure_mode = self._is_failure_blueprint()

        def bodies(u, v):
            """>body_pct check on exactly two candles (C2 and C3)."""
            if body <= 0:
                return True
            for c in (u, v):
                rng = float(c["high"]) - float(c["low"])
                if rng <= 0:
                    return False
                if abs(float(c["close"]) - float(c["open"])) / rng <= body:
                    return False
            return True

        # The OCL blueprint carries NO body filter (operator spec), so the
        # shape helpers below are pure colour tests. The failure blueprints
        # apply their >50% body rule separately (see _failure_body_filter).
        def seq(w):
            return self._is_red_green_claim(w[0], w[1], w[2])

        def seq_no_claim(w):
            return self._is_red_green(w[0], w[1], w[2])

        if self._is_failure_blueprint():
            # Core = a SELL pattern: C2 GREEN (conservative sell level = C2
            # open) then C3 RED (aggressive sell level = C3 open). C1 is
            # unconstrained - it may be green or red. C2 and C3 bodies must
            # both exceed min_body_pct_c23 (operator: >50%).
            # OLDEST window first so a candle remains AFTER the core to hold
            # the failure candle.
            use_body = self._failure_body_filter()
            for cand, pos in ((w4[:3] if w4 else None, [0, 1, 2]),
                              (w3, [1, 2, 3])):
                if not cand or not self._is_sell_sequence(
                        cand[0], cand[1], cand[2]):
                    continue
                if use_body and not bodies(cand[1], cand[2]):
                    continue
                if self._window_successor(w4, pos) is None:
                    # Nothing after the core in this frame -> the failure
                    # candle has not printed yet; keep looking.
                    continue
                self._core_positions = pos if w4 else [0, 1, 2]
                return cand
            return None

        # OCL blueprint: C1..C3 first (the C4 rule is what makes it valid),
        # then the shifted C2..C4 window, then classic C1..C3.
        if w4 is not None:
            c3_ok = bool(seq(w4[:3]))
            c4_ok = float(w4[3]["close"]) > float(w4[1]["open"])
            if (seq_no_claim(w4[:3]) and (c3_ok or c4_ok)):
                if not c3_ok:
                    self.log(
                        "[CORE] C3 did not close above C2 open; C4 does "
                        "-> C4 confirmation")
                self._core_positions = [0, 1, 2]
                return w4[:3]
        if seq(w3):
            self._core_positions = [1, 2, 3] if w4 else [0, 1, 2]
            return w3
        if w4 is None:
            return None
        # Sequence one candle later: C2..C4.
        if seq(w4[1:]):
            self.log("[CORE] OCL sequence found in C2..C4 (shifted window)")
            self._core_positions = [1, 2, 3]
            return w4[1:]
        return None

    @staticmethod
    def _is_sell_sequence(c1, c2, c3) -> bool:
        """SELL pattern traded by the failure blueprints.

        C2 is GREEN (its open is the conservative sell level, e.g. 120) and
        C3 is RED (its open is the aggressive sell level, e.g. 135). C1 is
        the preceding candle and is NOT colour-constrained - it may be green
        or red, it only frames the window.
        """
        return (c2["close"] > c2["open"] and c3["close"] < c3["open"])

    def _levels_are_fresh(self, w4, pos, c2, c3, fail_candle) -> bool:
        """True when BOTH sell levels are untouched before the break.

        Levels: conservative = C2 open, aggressive = C3 open.

        "Fresh" means no candle BETWEEN C3 and the failure candle already
        traded at or above either level. Such a candle would have consumed
        the level before the break, so the retest would not be the first
        retest of a virgin level. The failure candle itself is excluded - it
        is the break, so by definition it trades above the aggressive level.
        When the failure candle IS C3 there is no in-between candle at all
        and the sequence is fresh by construction.
        """
        if not w4 or fail_candle is c3:
            return True
        last = int(pos[2]) if pos else len(w4) - 1
        between = [c for c in w4[last + 1:]
                   if c is not None and c is not fail_candle and c is not c3]
        if not between:
            return True
        aggr = float(c3["open"])
        cons = float(c2["open"])
        top = max(aggr, cons)
        for c in between:
            if float(c["high"]) >= top:
                return False
        return True

    @staticmethod
    def _is_red_green(c1, c2, c3) -> bool:
        """C1 red, C2 red, C3 green (no body or reclaim test)."""
        return (c1["close"] < c1["open"] and c2["close"] < c2["open"]
                and c3["close"] > c3["open"])

    @staticmethod
    def _is_red_green_claim(c1, c2, c3) -> bool:
        """C1 red, C2 red, C3 green AND close of C3 above C2's open."""
        return (OCLKeylevelSignal._is_red_green(c1, c2, c3)
                and float(c3["close"]) > float(c2["open"]))

    @staticmethod
    def _is_sequence_without_reclaim(c1, c2, c3, min_body_pct: float) -> bool:
        """C1 red, C2 red, C3 green, >body C2/C3 (reclaim left to C4)."""
        if not (c1["close"] < c1["open"] and c2["close"] < c2["open"]
                and c3["close"] > c3["open"]):
            return False
        if min_body_pct > 0:
            for c in (c2, c3):
                rng = float(c["high"]) - float(c["low"])
                if rng <= 0:
                    return False
                if abs(float(c["close"]) - float(c["open"])) / rng \
                        <= min_body_pct:
                    return False
        return True

    def _c4_candidates(self, raw_window) -> list:
        """Closed candles AFTER the core, newest first.

        The failure/confirmation candle is any candle that came after the
        OCL sequence - which may be the last row, or the second-to-last when
        the core ends earlier. Newest first so the freshest invalidation
        wins.
        """
        out = []
        for c in reversed([w for w in raw_window if w is not None]):
            if c is self._core_end:
                break
            out.append(c)
        return out

    @staticmethod
    def _lowest_swing_candle(candles) -> Optional[dict]:
        """The candle with the LOWEST low in the given window (never None
        candles). This is the structural swing low the LOW blueprint levels
        against: its OPEN is the conservative BUY level."""
        live = [c for c in candles if c is not None]
        if not live:
            return None
        return min(live, key=lambda c: float(c["low"]))

    def _failure_side(self) -> Optional[str]:
        """Direction of the failure-retest ladder, or None for plain OCL.

        ALL blueprints in this family are BUY setups, so a retest is always
        approached from ABOVE.
        """
        if self._is_failure_blueprint():
            return "BUY"
        return None

    def _is_failure_blueprint(self) -> bool:
        """True for the two sell-failure blueprints."""
        return self.setup_mode in (SETUP_MODE_SELL_FAILURE_BOTTOM,
                                   SETUP_MODE_SELL_FAILURE_TOP)

    def _retest_level(self) -> Optional[float]:
        """Live retest target for the failure blueprints.

        TOP escalates down: aggressive first, conservative once the
        aggressive level has failed (both are BUY levels on the retest).
        BOTTOM trades its single level.
        """
        if self.setup_mode == SETUP_MODE_SELL_FAILURE_TOP:
            return self._conservative_level if self._oc_top_used \
                else self._aggressive_level
        return self._conservative_level  # BOTTOM: the single level

    def _failure_body_filter(self) -> bool:
        """Does THIS blueprint require the >50% body on C2 and C3?

        Operator spec: the plain OCL blueprint carries NO body filter; both
        sell-failure blueprints require >50% bodies on the failing C2 and C3.
        """
        return self.min_body_pct_c23 > 0

    @staticmethod
    def _window_successor(w4, pos) -> Optional[dict]:
        """Candle right AFTER a 3-candle window inside the 4-candle frame.

        ``pos`` are the window's indices in ``w4``. The candle AFTER the
        window is ``w4[pos[2] + 1]`` - for the C1..C3 window that is C4,
        for the (hypothetical) C2..C4 window there is none.
        """
        if not w4 or not pos:
            return None
        idx = int(pos[2]) + 1
        return w4[idx] if idx < len(w4) else None

    def _setup_stop_base(self) -> Optional[float]:
        """Structural base for the failure blueprints' stop-loss.

        BOTTOM (BUY): the failure lows - a close below them kills the setup.
        TOP   (SELL): the failure highs - a close above them kills the setup.
        """
        if self._setup_candle is None:
            return None
        side = self._failure_side()
        return (float(self._setup_candle["low"]) if side == "BUY"
                else float(self._setup_candle["high"]))

    def _normal_setup_ok(self, c2, c3, c4) -> bool:
        """OCL baseline: some candle closes above C2 open."""
        if not self.require_close_above_c2_open:
            return True
        if float(c3["close"]) > float(c2["open"]):
            return True
        if c4 is not None and float(c4["close"]) > float(c2["open"]):
            return True
        return False

    def _sell_failure_candle(self, c2, c3, c4):
        """Candle that BREAKS the SELL pattern by closing above it.

        SELL levels: conservative = C2 open, aggressive = C3 open. A candle
        that CLOSES ABOVE the nearer (aggressive) sell level has broken the
        pattern - that candle is the failure candle and the levels flip to
        BUY levels. C4 is checked first (the latest candle), then C3 itself.
        Returns None when the pattern has not been broken yet.
        """
        c3_open = float(c3["open"])
        c2_open = float(c2["open"])
        for label, cand in (("C4", c4), ("C3", c3)):
            if cand is None or cand is c3 and label == "C4":
                continue
            if float(cand["close"]) > c3_open:
                self.log(
                    f"[SELL FAILURE] {label} close "
                    f"{cand['close']:.2f} > aggressive sell level "
                    f"{c3_open:.2f} (conservative {c2_open:.2f}) "
                    f"-> levels FLIP, retest-from-above entry")
                return cand
        self.log(
            f"[SELL FAILURE] no close above {c3_open:.2f}; sell pattern "
            f"still intact")
        return None

    def _c23_bodies_ok(self, c2, c3) -> bool:
        """C2 and C3 must each be a >min_body_pct_c23 body/range candle."""
        for label, c in (("C2", c2), ("C3", c3)):
            rng = float(c["high"]) - float(c["low"])
            if rng <= 0:
                self.log(f"[BODY] {label} has zero range -> reject")
                return False
            frac = abs(float(c["close"]) - float(c["open"])) / rng
            if frac <= self.min_body_pct_c23:
                self.log(
                    f"[BODY] {label} body {frac:.2%} <= "
                    f"{self.min_body_pct_c23:.0%} -> reject")
                return False
        return True

    def _resolve_confirmation(self, c2, c3, c4):
        """Decide the confirmation candle for the setup.

        Returns (confirmed, candle, sell_failure).

        Rules:
          1. If require_close_above_c2_open is off -> C3 confirms.
          2. C3 close > C2 open                      -> C3 confirms.
          3. Else C4 close > C2 open (if allowed)    -> C4 confirms.
          4. BUY-ON-SELL-FAILURE: the sequence is an OCL SHORT setup whose
             conservative sell level (C3 open) is violated by a candle that
             CLOSES above it. That candle (C4 when present, else C3) marks
             the failure; the broken level is the new aggressive BUY level
             and the entry waits for a retest from ABOVE.
        """
        c2_open = float(c2["open"])
        c3_open = float(c3["open"])
        c3_ok = float(c3["close"]) > c2_open
        c4_ok = (c4 is not None and float(c4["close"]) > c2_open)

        if self.sell_failure_enabled:
            # Failure legs: C3 (or C4) closes ABOVE the conservative SELL
            # level = open of C3. If C3 itself already closed above it, the
            # short failed on C3; otherwise C4 is the failure candle.
            if float(c3["close"]) > c3_open:
                self.log(
                    f"[SELL FAILURE] C3 close {c3['close']:.2f} > "
                    f"sell level {c3_open:.2f}; retest-from-above entry")
                return True, c3, True
            if c4 is not None and float(c4["close"]) > c3_open:
                self.log(
                    f"[SELL FAILURE] C4 close {c4['close']:.2f} > "
                    f"sell level {c3_open:.2f}; retest-from-above entry")
                return True, c4, True
            self.log(
                "[SELL FAILURE] sell level not violated; no long setup")
            return False, None, False

        if not self.require_close_above_c2_open:
            return True, c3, False
        if c3_ok:
            self.log(
                f"[CONFIRM] C3 close {c3['close']:.2f} > C2 open "
                f"{c2_open:.2f}")
            return True, c3, False
        if c4 is not None and c4_ok:
            self.log(
                f"[CONFIRM] C3 close {c3['close']:.2f} <= C2 open "
                f"{c2_open:.2f}; C4 close {c4['close']:.2f} confirms")
            return True, c4, False
        self.log(
            f"[CONFIRM] no close above C2 open {c2_open:.2f} "
            f"(C3={c3['close']:.2f}, C4="
            f"{c4['close'] if c4 else 'n/a'})")
        return False, None, False

    @staticmethod
    def _last_bar_is_forming(response) -> bool:
        """True when the newest history row is still an OPEN candle.

        Detected only when the payload carries an explicit bar status;
        when it does not, the row is assumed closed (matches prior
        behaviour, so live/replay/backtest stay identical).
        """
        df = response.get("data")
        if df is None or len(df) == 0:
            return False
        try:
            row = df.iloc[-1]
        except Exception:  # noqa: BLE001
            return False
        for key in ("is_forming", "forming", "is_partial"):
            try:
                if key in row and bool(row[key]):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

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

            # ---- Risk gates FIRST (checked every poll) ----
            # Tier 1 - HTF structural stop: judged on the HTF CANDLE CLOSE
            # (the pattern timeframe decides invalidation, not the LTF).
            try:
                htf_resp = self.client.history(
                    symbol=self.symbol, exchange=self.exchange,
                    interval=self.htf_interval,
                    start_date=(ist_now() - timedelta(days=120))
                    .strftime("%Y-%m-%d"),
                    end_date=ist_now().strftime("%Y-%m-%d"),
                )
                htf_last = None
                if (htf_resp.get("status") == "success"
                        and htf_resp.get("data") is not None
                        and len(htf_resp["data"]) >= 1):
                    htf_last = self._extract_ohlc(htf_resp["data"].iloc[-1])
            except Exception as exc:
                self.log(f"[HTF SL CHECK] history error: {exc}")
                htf_last = None
            # FAILURE-RETEST ladder (all three failure blueprints are BUY):
            #   phase 1 (not broken): price must BREAK UP - close above the
            #            conservative SELL level (C3 open).
            #   phase 2 (broken):     entry only on the RETEST from above.
            #     SELL_FAILURE_TOP -> two-tier ladder: aggressive (broken C3
            #        open) first; if that level fails, the conservative level
            #        (C2 open) is the second entry.
            #     SELL_FAILURE_BOTTOM -> single level (lowest swing open).
            #     A close below the failure base kills the setup.
            if self._sell_failure_armed:
                break_level = self._broken_sell_level
                if not self._sell_failure_broken:
                    if break_level is None:
                        time.sleep(self.poll_seconds)
                        return None
                    if ohlc["close"] > break_level:
                        self._sell_failure_broken = True
                        self.log(
                            f"[{self.setup_mode} BREAK] close "
                            f"{ohlc['close']:.2f} broke {break_level:.2f} "
                            f"(BUY); entry armed on the RETEST")
                    else:
                        self.log(
                            f"[{self.setup_mode} WAIT] close "
                            f"{ohlc['close']:.2f} must break "
                            f"{break_level:.2f} (BUY)")
                        time.sleep(self.poll_seconds)
                        return None
                self._trigger_level = self._retest_level()
                # Setup death: an LTF close back BELOW the failure base.
                dead = (self._htf_stop_loss is not None
                        and ohlc["close"] < self._htf_stop_loss)
                if dead:
                    self.log(
                        f"[{self.setup_mode} DEAD] close "
                        f"{ohlc['close']:.2f} vs base "
                        f"{self._htf_stop_loss:.2f}; setup abandoned")
                    self._pattern_detected = False
                    self._trigger_level = None
                    self._htf_stop_loss = None
                    self._ltf_stop_loss = None
                    self._ltf_retry_count = 0
                    self._sell_failure_armed = False
                    self._sell_failure_broken = False
                    time.sleep(self.poll_seconds)
                    return None

            if self._check_htf_stop_loss(htf_last):
                self._pattern_detected = False
                self._trigger_level = None
                self._htf_stop_loss = None
                self._ltf_stop_loss = None
                self._ltf_retry_count = 0
                self._sell_failure_armed = False
                self._sell_failure_broken = False
                time.sleep(self.poll_seconds)
                return None

            # Tier 2 - LTF entry stop: judged on the LTF close. The pattern stays
            # valid; the next entry level drops below this stop. When the
            # retry cap is reached, the setup is abandoned (pattern NOT
            # invalidated - price is still above the HTF structural low).
            # SELL_FAILURE_TOP ESCALATION: once the aggressive BUY level is
            # used (a trade fired there and its LTF stop was hit), the
            # blueprint drops to its conservative level. When BOTH levels
            # have failed the operator switches to the BOTTOM blueprint -
            # logged explicitly so the hand-off is never silent.
            if (self._is_failure_blueprint() and self._sell_failure_armed
                    and self._sell_failure_broken
                    and self.setup_mode == SETUP_MODE_SELL_FAILURE_TOP
                    and self._oc_top_used and not self._oc_bottom_used):
                self.log(
                    f"[ESCALATE] SELL_FAILURE_TOP aggressive "
                    f"{self._aggressive_level:.2f} AND conservative "
                    f"{self._conservative_level:.2f} both failed -> "
                    f"look for SELL_FAILURE_BOTTOM (lowest swing open)")
                self._oc_bottom_used = True
                self.setup_mode = SETUP_MODE_SELL_FAILURE_BOTTOM
                self._low_swing_level = self._conservative_level
                self._trigger_level = self._conservative_level

            if self._check_ltf_stop_loss(ohlc):
                self._ltf_retry_count += 1
                if (self.setup_mode == SETUP_MODE_SELL_FAILURE_TOP
                        and self._sell_failure_armed
                        and self._sell_failure_broken
                        and not self._oc_top_used):
                    # The aggressive level did not hold -> step to the
                    # conservative BUY level instead of abandoning.
                    self._oc_top_used = True
                    self._ltf_stop_loss = None
                    self._trigger_level = self._conservative_level
                    self.log(
                        f"[SELL_FAILURE_TOP] aggressive level failed -> "
                        f"conservative BUY level armed at "
                        f"{self._conservative_level:.2f}")
                    time.sleep(self.poll_seconds)
                    return None
                if self._ltf_retry_count >= self.max_ltf_retries:
                    self.log(
                        f"[SETUP ABANDONED] {self.max_ltf_retries} LTF stops "
                        f"hit; HTF-SL {self._htf_stop_loss:.2f} not broken - "
                        f"waiting for a new HTF pattern"
                    )
                    self._pattern_detected = False
                    self._trigger_level = None
                    self._htf_stop_loss = None
                    self._ltf_stop_loss = None
                    self._ltf_retry_count = 0
                    time.sleep(self.poll_seconds)
                    return None
                # Step the ENTRY LEVEL down below the LTF stop that just
                # fired - the next touch must happen at a cheaper price.
                self._trigger_level = min(
                    self._trigger_level,
                    ohlc["close"],
                )
                self._ltf_stop_loss = None
                # fall through: the monitor keeps watching at the new level
                self.log(
                    f"[RETRY {self._ltf_retry_count}/{self.max_ltf_retries}] "
                    f"entry level re-armed at {self._trigger_level:.2f}"
                )
                time.sleep(self.poll_seconds)
                return None

            # ---- Post-entry management: ATR/DV-ATR trail + breakeven ----
            if self._fired:
                if self.atr_trail_enabled or self.dvs_enabled:
                    self._update_trailing_stop(df)
                self._update_breakeven(ltp)
                # Traditional SL stays the fallback: never let the live
                # stop fall below it.
                if self._traditional_sl is not None:
                    self._ltf_stop_loss = max(
                        self._ltf_stop_loss or 0.0, self._traditional_sl)
                if self._dvs_active:
                    self.log(
                        f"[DV-ATR] stop={self._dvs_stop:.2f} scale="
                        f"{self._dvs_scale:.2f} fallback_SL="
                        f"{self._traditional_sl:.2f}"
                    )
                # Trail exit check (DYNAMIC mode has no hard target, so
                # the trail is the exit; in FIXED it may pre-empt it).
                if ((self.atr_trail_enabled or self.dvs_enabled)
                        and self._check_trailing_stop(ohlc)):
                    self.log("[EXIT] DV/ATR trail hit; setup complete")
                    self._reset_trail()
                    self._fired = False
                    time.sleep(self.poll_seconds)
                    return None

            # ---- Condition 3: LTF RRG pattern gate (last 3 LTF candles) ----
            if self.ltf_pattern_enabled and not self._check_ltf_rrg(df):
                self.log("[LTF GATE] no RRG on last 3 LTF candles; waiting")
                time.sleep(self.poll_seconds)
                return None

            # ---- Condition 4: Hilega-Milega bullish gate (RSI > EMA) ----
            if self.hm_enabled and not self._hilega_milega_bullish(df, label="LTF"):
                self.log("[HM GATE] Hilega-Milega not bullish; waiting")
                time.sleep(self.poll_seconds)
                return None

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
    # [LTF RRG GATE] Condition 3: RRG pattern on last 3 LTF candles
    # ============================================================
    def _check_ltf_rrg(self, df) -> bool:
        """Require an RRG (Red-Red-Green) on the last three LTF candles.

        The HTF scan finds the setup; this gate demands the same bullish
        structure on the execution timeframe before the LTF touch counts.
        BUY-only: the strategy never trades the bearish GRR structure.
        """
        try:
            if df is None or len(df) < 3:
                self.log("[LTF RRG] insufficient LTF candles (<3)")
                return False
            c1 = self._extract_ohlc(df.iloc[-3])
            c2 = self._extract_ohlc(df.iloc[-2])
            c3 = self._extract_ohlc(df.iloc[-1])
            ok = validate_pattern(c1, c2, c3, "RRG", 0.0)
        except Exception as exc:
            self.log(f"[LTF RRG] error: {exc}")
            return False
        if ok:
            self.log("[LTF RRG] OK last 3 LTF candles form RRG (BUY)")
        return ok

    # ============================================================
    # [HILEGA MILEGA GATE] Condition 4: NK Hilega-Milega indicator
    # ============================================================
    def _hilega_milega_bullish(self, df, label: str = "LTF",
                               apply_oversold: Optional[bool] = None) -> bool:
        """Hilega-Milega gate: RSI > EMA(RSI), optional oversold cap.

        ``apply_oversold`` controls the oversold-zone requirement:
          * True  -> RSI must ALSO be <= hm_oversold_level (recovery from
                     oversold). Used on the LTF by default.
          * False -> the cross alone is enough. Used on the HTF by default
                     (the operator dropped the oversold cap on the HTF:
                     requiring RSI<=cap on a monthly gate filters out the
                     strongest post-RRG uptrends).
          * None  -> use hm_htf_oversold_enabled for HTF / always True LTF.
        """
        try:
            closes = [float(c) for c in df["close"].tolist()]
        except (KeyError, TypeError, ValueError, AttributeError):
            self.log(f"[HM:{label}] no usable close series")
            return False

        need = self.hm_rsi_period + self.hm_ema_period + 2
        if len(closes) < need:
            self.log(f"[HM:{label}] insufficient bars ({len(closes)} < {need})")
            return False

        rsi = hilega_milega_rsi(closes, rsi_period=self.hm_rsi_period)
        ema_line = ema(rsi, self.hm_ema_period)

        # Last fully-defined index across the two series.
        i = min(len(rsi), len(ema_line)) - 1
        if i < 0 or rsi[i] is None or ema_line[i] is None:
            self.log(f"[HM:{label}] last bar undefined (warming up); waiting")
            return False

        r, e = rsi[i], ema_line[i]
        if apply_oversold is None:
            apply_oversold = (label != "HTF") or self.hm_htf_oversold_enabled
        # Oversold-only entry (when enabled): the BUY must fire from the
        # oversold zone (RSI <= hm_oversold_level, default 30). Combined
        # with RSI > EMA this is the recovery cross FROM oversold. When the
        # cap is disabled (HTF default), the RSI>EMA cross alone is enough.
        oversold_ok = r <= self.hm_oversold_level
        bullish = r > e and (oversold_ok or not apply_oversold)
        if bullish:
            why = ("RSI > EMA and in/below oversold" if apply_oversold
                   else "RSI > EMA (no oversold cap on HTF)")
        elif r > e:
            why = "BLOCKED (RSI above oversold; already ran up)"
        else:
            why = "NOT bullish (RSI <= EMA)"
        self.log(
            f"[HM:{label}] RSI={r:.2f} EMA(RSI)={e:.2f} "
            f"oversold_lvl={self.hm_oversold_level} -> {why}"
        )
        return bullish

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
            # resolved from the DATA during _refresh_daily_band (see below),
            # not from the wall clock: anchoring on ist_now() would point the
            # window at today's date even when replaying older data, so the
            # band would cover the wrong candles entirely.
            self._touch_date = None
            self._touch_vwap.reset(
                level=self._trigger_level,
                action=self.action,
                start_bar=0,
            )
            self.log(
                f"[CONFIRM ARMED] level={self._trigger_level:.2f} "
                f"action={self.action} bar={bar} - awaiting ROC flip"
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
        if self._touch_date is None:
            # First framing pass: bind the band anchor to the LAST daily
            # candle present in the data (the candle that is live at the
            # moment of the touch). This is data-derived rather than
            # clock-derived, so replay, backtest and live all anchor to the
            # same real candle.
            self._touch_date = self._last_frame_date(frame)
            self.log(f"[BAND] touch anchored to data date={self._touch_date}")

        if self._touch_date:
            # Keep only daily candles dated on or after the touch candle.
            # This is the "from the touch candle" window - NOT the whole
            # daily history, and not a positional slice of a frame whose
            # indices do not correspond to LTF bar numbers.
            dates = self._frame_dates(frame)
            if dates:
                try:
                    start = next(
                        i for i, d in enumerate(dates)
                        if d[:10] >= self._touch_date
                    )
                except StopIteration:
                    start = len(frame)      # touch newer than all history
            self.log(
                f"[BAND] anchoring from touch date={self._touch_date} "
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

    def _frame_dates(self, frame) -> list:
        """Extract YYYY-MM-DD date strings from a candle frame.

        Tries a datetime-like column first, then the frame index. Returns an
        empty list when the frame carries no usable dates, which makes the
        caller fall back to using the whole frame rather than guessing.
        """
        for key in ("datetime", "date", "timestamp", "time"):
            try:
                col = frame[key]
            except (KeyError, TypeError):
                continue
            try:
                vals = col.astype(str).str[:10].tolist()
            except (AttributeError, TypeError):
                try:
                    vals = [str(v)[:10] for v in col.tolist()]
                except (AttributeError, TypeError):
                    continue
            if vals and any(v.strip() for v in vals):
                return vals
        try:
            return [str(v)[:10] for v in frame.index.tolist()]
        except (AttributeError, TypeError):
            return []

    def _last_frame_date(self, frame) -> Optional[str]:
        """The data date of the newest candle in the frame."""
        dates = self._frame_dates(frame)
        for d in reversed(dates):
            if d and d.strip():
                return d
        return None

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
    # [STOP LOSS CHECK] Two-tier: HTF structural SL + LTF entry SL
    # ============================================================
    def _check_htf_stop_loss(self, htf_candle) -> bool:
        """HTF stop: an HTF candle CLOSES below the pattern's structural low.

        This is the pattern-invalidation stop and it is judged on the HTF
        candle's own close - an LTF close below the level does NOT kill the
        pattern (only the LTF entry stop reacts to that). A wick through
        does not count either. When it fires, the whole RRG setup is dead
        and the scanner waits for a NEW HTF pattern.
        """
        sl = self._htf_stop_loss
        if sl is None or htf_candle is None:
            return False
        if self.action == "BUY" and htf_candle["close"] < sl:
            self.log(
                f"[HTF SL HIT] HTF candle closed {htf_candle['close']:.2f} "
                f"< HTF-SL {sl:.2f} -> pattern INVALIDATED; "
                f"waiting for a new HTF pattern"
            )
            return True
        if self.action == "SELL" and htf_candle["close"] > sl:
            self.log(
                f"[HTF SL HIT] HTF candle closed {htf_candle['close']:.2f} "
                f"> HTF-SL {sl:.2f} -> pattern INVALIDATED; "
                f"waiting for a new HTF pattern"
            )
            return True
        return False

    def _check_ltf_stop_loss(self, candle) -> bool:
        """LTF stop: an LTF candle CLOSES below the current entry's SL.

        This is a per-entry stop. When it fires (and the HTF structural
        stop has NOT), the setup stays alive and the NEXT entry level is
        stepped BELOW this stop-loss. Retries are capped by
        ``max_ltf_retries``; hitting the cap abandons the setup without
        killing the pattern.
        """
        sl = self._ltf_stop_loss
        if sl is None:
            return False
        hit = (self.action == "BUY" and candle["close"] < sl) or (
            self.action == "SELL" and candle["close"] > sl)
        if hit:
            self.log(
                f"[LTF SL HIT] LTF close {candle['close']:.2f} vs LTF-SL "
                f"{sl:.2f} (retry {self._ltf_retry_count}/"
                f"{self.max_ltf_retries}); next entry level steps away "
                f"from this stop"
            )
            return True
        return False

    # ============================================================
    # [BREAKEVEN] Ratchet the LTF stop to entry+cost past breakeven_rr
    # ============================================================
    def _update_breakeven(self, ltp: float) -> None:
        """Once price reaches breakeven_rr x risk, lock in entry+cost.

        Applies to the live LTF stop (works with both exit modes: in
        FIXED it protects while the target is pending; in DYNAMIC it
        floors the trail so the trailing stop can never give back the
        breakeven). Idempotent - arms once per trade.
        """
        if self._breakeven_armed or not self._fired:
            return
        entry_sl = self._ltf_stop_loss or self._htf_stop_loss
        if entry_sl is None or self._trigger_level is None:
            return
        risk = self._trigger_level - entry_sl
        if risk <= 0:
            return
        be_trigger = self._trigger_level + risk * self.breakeven_rr
        be_floor = self._trigger_level * (
            1.0 + self.round_trip_cost_pct / 100.0
        )
        if not self._breakeven_armed and ltp >= be_trigger:
            if (self._ltf_stop_loss is None or be_floor > self._ltf_stop_loss):
                self._ltf_stop_loss = be_floor
            self._breakeven_armed = True
            self.log(
                f"[BREAKEVEN] price {ltp:.2f} >= {be_trigger:.2f} "
                f"({self.breakeven_rr}R); SL locked to entry+cost "
                f"{be_floor:.2f}"
            )
        # [BREAKEVEN + DV-ATR] After the cost floor is armed, the DV-ATR
        # may LIFT the stop even higher. The protective stop never falls
        # back to the cost floor once lifted.
        if self._breakeven_armed and self._dvs_stop is not None:
            if self._dvs_stop > (self._ltf_stop_loss or 0.0):
                self._ltf_stop_loss = self._dvs_stop
                self.log(
                    f"[DV-ATR LIFT] breakeven stop raised to "
                    f"{self._dvs_stop:.2f} (was {be_floor:.2f})"
                )

    # ============================================================
    # [ATR TRAILING STOP] Dynamic ratcheting exit
    # ============================================================
    def _update_trailing_stop(self, df) -> None:
        """Ratchet the trailing stop after entry (BUY only).

        trail = max(prev_trail, highest_close_since_entry - mult * ATR).
        Never moves DOWN; never below the initial LTF entry stop. Called
        on every LTF poll while a position from this setup is open.
        """
        if not self.atr_trail_enabled or not self._fired:
            return
        try:
            highs = [float(h) for h in df["high"].tolist()]
            lows = [float(l) for l in df["low"].tolist()]
            closes = [float(c) for c in df["close"].tolist()]
        except (KeyError, TypeError, ValueError, AttributeError):
            return
        if len(closes) < self.atr_period + 1:
            return
        atr_series = atr(highs, lows, closes, period=self.atr_period)
        a = atr_series[-1]
        if a is None or a <= 0:
            return
        c = closes[-1]
        if self._highest_close is None or c > self._highest_close:
            self._highest_close = c
        # [DV-ATR] volatility-adaptive distance. Scale > 1 widens the
        # trail in high vol, < 1 tightens it in low vol.
        scale = 1.0
        distance = self.atr_multiplier * a
        if self.dvs_enabled:
            scale = self._dvs_scale_from_series(highs, lows, closes)
            self._dvs_scale = scale
            distance = self.atr_multiplier * a * scale
        # Traditional stop loss = fallback floor. It sits BELOW the
        # DV-ATR distance so the DV-ATR only takes over once it has lifted
        # above the classic stop.
        self._update_traditional_stop(a)
        candidate = max(self._highest_close - distance, 0.0)
        base = self._ltf_stop_loss or self._htf_stop_loss or 0.0
        new_stop = max(candidate, base)
        if self._trail_stop is None or new_stop > self._trail_stop:
            if self._trail_stop is None:
                self._trail_stop = new_stop
                self._trail_active = True
                self.log(
                    f"[ATR TRAIL] armed at {new_stop:.2f} "
                    f"(highest_close={self._highest_close:.2f}, "
                    f"ATR={a:.2f}, mult={self.atr_multiplier})"
                )
            else:
                self._trail_stop = new_stop
                self.log(
                    f"[ATR TRAIL] ratcheted UP to {new_stop:.2f} "
                    f"(highest_close={self._highest_close:.2f}, ATR={a:.2f})"
                )
        if self.dvs_enabled:
            # DV-ATR never sits above the price-implied trail; it ratchets
            # up and only counts once it clears the traditional fallback.
            self._sync_dvs_stop(candidate)

    # ============================================================
    # [DV-ATR] Dynamic Volatility Smoothing ATR
    # ============================================================
    def _dvs_scale_from_series(self, highs, lows, closes) -> float:
        """Volatility scale = 1 + sens*(realized/typical - 1), clamped.

        realized vol  = stdev(TR) / mean(TR) over ``atr_period`` bars.
        typical  vol  = mean of that ratio over ``dvs_reference_period``
                        bars (causal; only closed history is used).
        A flat/undefined window returns 1.0 (classic ATR behaviour).
        """
        p = self.atr_period
        ref_n = max(self.dvs_reference_period, p + 2)
        if len(closes) < ref_n + 1:
            return 1.0
        trs = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
            trs.append(float(tr))
        if len(trs) < ref_n:
            return 1.0
        def _cv(window):
            n = len(window)
            if n < 2:
                return None
            m = sum(window) / n
            if m <= 0:
                return None
            var = sum((x - m) ** 2 for x in window) / (n - 1)
            return (var ** 0.5) / m
        hist_cv = []
        for j in range(len(trs) - ref_n, len(trs) - p):
            cv = _cv(trs[j:j + p])
            if cv is not None:
                hist_cv.append(cv)
        cur = _cv(trs[-p:])
        # exclude the current window from its own reference
        ref_vals = [v for v in hist_cv if v is not None]
        if cur is None or not ref_vals:
            return 1.0
        ref = sum(ref_vals) / len(ref_vals)
        if ref <= 0:
            return 1.0
        raw = 1.0 + self.dvs_sensitivity * (cur / ref - 1.0)
        lo = 1.0 / self.dvs_max_scale
        hi = self.dvs_max_scale
        return max(lo, min(hi, raw))

    def _update_traditional_stop(self, a: float) -> None:
        """Traditional SL = entry - stop_fallback_atr_mult * ATR.

        This is the CLASSIC stop, kept as the hard fallback FLOOR beneath
        the DV-ATR. It is computed once per trade and then frozen, so the
        DV-ATR trail has a fixed level to sit ABOVE.
        """
        if self._traditional_sl_frozen:
            return
        entry = self._entry_price or self._trigger_level
        if entry is None or a <= 0:
            return
        self._traditional_sl = entry - self.stop_fallback_atr_mult * a
        self._traditional_sl_frozen = True
        self.log(
            f"[TRAD SL] fallback = {self._traditional_sl:.2f} "
            f"(entry {entry:.2f} - {self.stop_fallback_atr_mult}*ATR {a:.2f})"
        )

    def _sync_dvs_stop(self, dv_atr_level: float) -> None:
        """Ratchet the DV-ATR stop UP; log when it clears the fallback."""
        if dv_atr_level <= 0:
            return
        trad = self._traditional_sl
        prev = self._dvs_stop
        if prev is not None and dv_atr_level <= prev:
            return
        self._dvs_stop = dv_atr_level
        if not self._dvs_active and trad is not None and dv_atr_level > trad:
            self._dvs_active = True
            self.log(
                f"[DV-ATR] ACTIVE: stop {dv_atr_level:.2f} lifted above "
                f"traditional SL {trad:.2f} (scale={self._dvs_scale:.2f})"
            )
        elif self._dvs_active:
            self.log(
                f"[DV-ATR] ratcheted to {dv_atr_level:.2f} "
                f"(scale={self._dvs_scale:.2f})"
            )

    def protective_stop(self) -> Optional[float]:
        """Live protective stop = max(traditional SL, DV-ATR trail).

        The DV-ATR always sits ABOVE the traditional fallback stop once it
        has lifted, so the traditional level remains the safety floor.
        """
        levels = [v for v in (self._traditional_sl, self._dvs_stop)
                  if v is not None]
        return max(levels) if levels else None

    def effective_size_factor(self) -> float:
        """Position-size multiplier: DV-ATR active -> sizing factor."""
        if self.dvs_enabled and self._dvs_active:
            return self.dvs_atr_sizing_factor
        return 1.0

    def _check_trailing_stop(self, candle) -> bool:
        """Trail exit: an LTF CLOSE below the ratcheted trail stop."""
        if not self._trail_active or self._trail_stop is None:
            return False
        if candle["close"] < self._trail_stop:
            self.log(
                f"[ATR TRAIL EXIT] close {candle['close']:.2f} < "
                f"trail {self._trail_stop:.2f} -> exit signal"
            )
            return True
        return False

    # ============================================================
    # [TOUCH CHECK] Price-vs-Level Comparison
    # ============================================================
    def _check_touch(self, ltp, candle):
        """Check if price touched the trigger level."""
        level = self._trigger_level
        buffer = self.ltf_touch_buffer

        if self.action == "BUY":
            if candle["low"] <= (level + buffer):
                self.log(f"[TOUCH] OK Bullish support! [{self.setup_mode}]")
                return True
        else:
            if candle["high"] >= (level - buffer):
                self.log(
                    f"[TOUCH] OK resistance touch! [{self.setup_mode}]")
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

        # Per-entry LTF stop: the structural (HTF) stop is the pattern floor;
        # the trade's own stop sits at the HTF floor if this is the first
        # entry, otherwise... it is set by the retry ladder (see detect flow).
        # Keep the LTF stop at the HTF structural stop when undefined so a
        # first fire always carries a defined SL.
        entry_sl = self._ltf_stop_loss or self._htf_stop_loss
        if entry_sl is None:
            entry_target = None
        elif self.action == "BUY":
            entry_target = (ltp + (ltp - entry_sl) * self.target_rr
                            if entry_sl < ltp else None)
        else:
            entry_target = (ltp - (entry_sl - ltp) * self.target_rr
                            if entry_sl > ltp else None)
        # Resolve the EFFECTIVE exit mode. AUTO: an entry that touched the
        # aggressive level (price came down to open-of-C2 zone) exits on
        # the FIXED target; an entry that reached the deeper conservative
        # level (min-low zone, incl. ladder re-entries) rides the ATR
        # trailing stop (DYNAMIC - no hard target).
        if self.exit_mode == "AUTO":
            if (self._aggressive_level is not None
                    and ltp >= self._aggressive_level * 0.995):
                self._entry_exit_mode = "FIXED"
            elif (self._conservative_level is not None
                  and ltp <= self._conservative_level * 1.005):
                self._entry_exit_mode = "DYNAMIC"
            else:
                self._entry_exit_mode = "FIXED"   # fallback: take the target
        else:
            self._entry_exit_mode = self.exit_mode
        self.log(
            f"[EXIT MODE] entry at {ltp:.2f} -> "
            f"{self._entry_exit_mode} "
            f"(aggr={self._aggressive_level}, cons={self._conservative_level})"
        )
        # In the resolved DYNAMIC mode the hard target is suppressed - the
        # ATR trailing stop is the exit.
        if self._entry_exit_mode == "DYNAMIC":
            entry_target = None
        # Arm the per-entry LTF stop so subsequent polls watch for its
        # close-below hit (retry ladder). The HTF stop stays unchanged.
        self._ltf_stop_loss = entry_sl
        # Start the ATR trailing baseline at entry.
        self._highest_close = ltp
        self._trail_stop = None
        self._trail_active = False
        self._breakeven_armed = False
        self._dvs_stop = None
        self._dvs_active = False
        self._dvs_scale = 1.0
        # Traditional fallback SL is derived on the first post-entry poll
        # as entry - stop_fallback_atr_mult * ATR (frozen afterwards).
        self._traditional_sl = None
        self._traditional_sl_frozen = False

        intent = {
            "action": self.action,
            "quantity": fire_qty,
            "tag": self.config.get("strategy_label", "OCL-Keylevel"),
            "reason": (
                f"OCL-{self.pattern_type} fire #{self._fire_count}/{self._max_label()} "
                f"at {ltp:.2f} (level={self._trigger_level:.2f}, mode={self.exec_mode})"
            ),
            "stop_loss": entry_sl,
            "htf_stop_loss": self._htf_stop_loss,
            "target": entry_target,
            "_meta": {
                "fire_count": self._fire_count,
                "exec_mode": self.exec_mode,
                "trigger_level": self._trigger_level,
                "pattern": self.pattern_type,
                "stop_loss": entry_sl,
                "htf_stop_loss": self._htf_stop_loss,
                "target_rr": self.target_rr,
                "exit_mode": self._entry_exit_mode,
                "ltf_retry_count": self._ltf_retry_count,
                "setup_mode": ("SELL_FAILURE" if self._sell_failure_armed
                               else "OCL"),
                "aggressive_level": self._aggressive_level,
                "conservative_level": self._conservative_level,
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

        # Entry intent handed off. Retain this signal instance to supervise
        # exits; don't apply ONE_SHOT exhaustion until a close intent returns.
        self._position_open = True
        self._position_quantity = fire_qty
        self._entry_price = float(ltp)
        self._position_stop = entry_sl
        self._position_target = entry_target
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
            self.log("[MODE] ONE_SHOT -> no additional entries; supervising exit")

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
                self._sell_failure_armed = False
                self._sell_failure_broken = False
                self.log("[REARM] Will re-scan HTF for new pattern on next cycle")

        elif self.exec_mode == EXEC_MODE_AUTO:
            if self.rearm_on_fire:
                self._pattern_detected = False
                self._trigger_level = None
                self._sell_failure_armed = False
                self._sell_failure_broken = False
                self.log("[AUTO] Re-armed -> will re-scan HTF for new pattern")

        return intent
    def _monitor_open_position(self):
        """Watch an open OCL position and emit a strategy-managed close."""
        try:
            end_date = ist_now().strftime("%Y-%m-%d")
            start_date = (ist_now() - timedelta(days=120)).strftime("%Y-%m-%d")
            htf_resp = self.client.history(
                symbol=self.symbol, exchange=self.exchange,
                interval=self.htf_interval, start_date=start_date,
                end_date=end_date)
            ltf_resp = self.client.history(
                symbol=self.symbol, exchange=self.exchange,
                interval=self.ltf_interval,
                start_date=(ist_now() - timedelta(days=3)).strftime("%Y-%m-%d"),
                end_date=end_date)
            if (htf_resp.get("status") != "success"
                    or ltf_resp.get("status") != "success"):
                self.log("[POSITION MONITOR] history unavailable; no exit signal")
                time.sleep(self.poll_seconds)
                return None
            htf_df, ltf_df = htf_resp.get("data"), ltf_resp.get("data")
            if htf_df is None or len(htf_df) < 1 or ltf_df is None or len(ltf_df) < 1:
                time.sleep(self.poll_seconds)
                return None
            htf_candle = self._extract_ohlc(htf_df.iloc[-1])
            ltf_candle = self._extract_ohlc(ltf_df.iloc[-1])
            ltp = ltf_candle["close"]

            reason = None
            if self._check_htf_stop_loss(htf_candle):
                reason = "HTF structural stop: pattern invalidated"
            elif (self._position_stop is not None
                  and ltp <= self._position_stop):
                reason = "LTF stop-loss close"
            else:
                if self.atr_trail_enabled or self.dvs_enabled:
                    self._update_trailing_stop(ltf_df)
                self._update_breakeven(ltp)
                if self._breakeven_armed:
                    self._position_stop = max(
                        self._position_stop or 0.0,
                        self._ltf_stop_loss or 0.0)
                # [DV-ATR] traditional fallback floor + DV-ATR lift
                ps = self.protective_stop()
                if ps is not None:
                    self._position_stop = max(
                        self._position_stop or 0.0, ps)
                if (self._entry_exit_mode == "FIXED"
                        and self._position_target is not None
                        and ltp >= self._position_target):
                    reason = "fixed RR target"
                elif ((self.atr_trail_enabled or self.dvs_enabled)
                      and self._check_trailing_stop(ltf_candle)):
                    reason = ("DV-ATR trailing stop" if self._dvs_active
                              else "ATR trailing stop")

            if reason is None:
                self.log(
                    f"[POSITION MONITOR] {self.symbol} close={ltp:.2f} "
                    f"SL={self._position_stop} TP={self._position_target} "
                    f"trail={self._trail_stop}"
                )
                time.sleep(self.poll_seconds)
                return None
            self.log(f"[POSITION EXIT] {self.symbol}: {reason} at {ltp:.2f}")
            close_intent = {
                "action": "SELL",
                "quantity": self._position_quantity,
                "tag": self.config.get("strategy_label", "OCL-Keylevel"),
                "reason": reason,
                "stop_loss": None,
                "target": None,
                "_meta": {
                    "position_exit": True,
                    "exit_reason": reason,
                    "entry_price": self._entry_price,
                    "exit_mode": self._entry_exit_mode,
                    "htf_stop_loss": self._htf_stop_loss,
                    "final_stop_loss": self._position_stop,
                    "target": self._position_target,
                },
            }
            self._position_open = False
            self._position_quantity = 0
            self._fired = False
            self._reset_trail()
            self._pattern_detected = False
            self._trigger_level = None
            self._htf_stop_loss = None
            self._ltf_stop_loss = None
            return close_intent
        except Exception as exc:  # noqa: BLE001
            self.log(f"[POSITION MONITOR] error: {exc!r}")
            time.sleep(self.poll_seconds)
            return None
    def _reset_trail(self) -> None:
        """Clear the ATR / DV-ATR trailing state (setup ended or exit)."""
        self._trail_active = False
        self._trail_stop = None
        self._highest_close = None
        self._dvs_stop = None
        self._dvs_active = False
        self._dvs_scale = 1.0
        self._traditional_sl = None
        self._traditional_sl_frozen = False

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
