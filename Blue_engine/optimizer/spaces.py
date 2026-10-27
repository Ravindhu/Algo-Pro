"""
optimizer/spaces.py - ready-made search spaces for this project's strategies.

WHY THESE LIVE IN CODE AND NOT IN A NOTEBOOK
--------------------------------------------
A search space *is* a research decision, and research decisions need to be
versioned, reviewed and diffed. The bounds below are not arbitrary: each one is
derived from a documented behaviour of the module it points at, and the reason
is written on the parameter. If a bound changes, the change shows up in git
next to the strategy commit that motivated it.

TWO SOURCES OF TRUTH
--------------------
1. The spaces defined here (the shipped defaults).
2. ``data/optimizer/param_space.json`` - an optional on-disk override so a desk
   can widen a bound or add a parameter without editing Python. When the file
   exists and names a space, it *replaces* the built-in one for that name.
   Replacement rather than merge is deliberate: a silent merge of two bound
   sets would make it impossible to answer "what did this study actually
   search?" from the study record alone.

SPACES SHIPPED
--------------
``ocl_keylevel``        10 parameters - the AGENTS.md "10-parameter" workflow.
                        Names and defaults are taken verbatim from
                        ``alpha/Statergy/ocl_keylevel.py``, so a default-valued
                        trial reproduces today's production behaviour exactly.
``pov_slicer``          6 parameters - the execution slicer's rate/band knobs.
``portfolio_rebalance`` 4 parameters - the cost/turnover lever from
                        ``portfolio/optim/benchmark.py`` (where the code itself
                        notes the cost spread between rebalance policies was
                        ~310 bps versus ~0.04 Sharpe between optimisers - so
                        this is the lever that actually moves P&L).
``synthetic_demo``      6 parameters - a space whose true optimum is KNOWN, used
                        by the tests to assert the optimiser finds the peak
                        instead of merely running without error.

WHAT IS DELIBERATELY *NOT* SEARCHABLE
-------------------------------------
``symbol``, ``exchange``, ``action`` and ``quantity``. Those are not parameters,
they are *the trade*. Optimising a symbol choice over a few years of one
instrument is how a backtest turns into a data-mining exercise, and optimising
quantity is what the risk layer owns - not the alpha layer.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from . import logger
from .search_space import ParamSpec, SearchSpace, make_space

__all__ = ["SPACES", "builtin_space", "load_space", "available_spaces",
           "register_space"]


# --------------------------------------------------------------------------- #
# OCL keylevel - the 10-parameter workflow
# --------------------------------------------------------------------------- #

_OCL_KEYLEVEL: List[Dict[str, Any]] = [
    # --- pattern recognition (what counts as the setup) ------------------- #
    # AUDIT (2026-10-22, F-24): ``min_body_pct`` is NOT searchable.
    #
    # It used to be declared here as float[0.10, 0.70], but
    # ``alpha/signals/_pattern.py:88-97`` removed the body-size filter by
    # operator instruction and now accepts-and-ignores the argument
    # (``_ = min_body_pct``). A tuner searching this dimension therefore
    # evaluated N identical strategies and spent its budget on a flat axis,
    # which also made the importance plot report it as "unimportant" for a
    # structural reason rather than a statistical one.
    #
    # It is deliberately NOT listed below. If the filter is ever restored in
    # ``_pattern.py``, re-add it here at the same time.
    dict(name="pattern_type", kind="categorical", choices=["RRG", "GRR"],
         default="RRG",
         doc="RRG = bullish reversal (dip then reclaim). GRR = bearish."),
    dict(name="htf_candles", kind="int", low=3, high=10, default=5,
         doc="How many higher-timeframe candles the pattern is scanned over."),

    # --- level & touch (where the trade is armed) ------------------------- #
    dict(name="ltf_touch_buffer", kind="float", low=0.00, high=0.30,
         default=0.05,
         doc="How close price must come to the keylevel to count as a touch, "
             "as a fraction of price. 0 = exact touch only, which almost never "
             "fills on Indian index levels."),
    dict(name="trigger_level_mode", kind="categorical",
         choices=["CONSERVATIVE", "AGGRESSIVE"], default="CONSERVATIVE",
         doc="CONSERVATIVE buys at C3 open, AGGRESSIVE buys at C3 close "
             "(see aggressive/conservative_level_source)."),
    dict(name="aggressive_level_source", kind="categorical",
         choices=["C3_CLOSE", "LEGACY"], default="C3_CLOSE",
         doc="AGGRESSIVE level: close of C3 (default) or legacy open of C2."),
    dict(name="conservative_level_source", kind="categorical",
         choices=["C3_OPEN", "LEGACY"], default="C3_OPEN",
         doc="CONSERVATIVE level: open of C3 (default) or legacy min low C2/C3."),
    dict(name="require_close_above_c2_open", kind="categorical",
         choices=[True, False], default=True,
         doc="Setup requires a candle to CLOSE above C2's open."),
    dict(name="allow_c4_confirmation", kind="categorical",
         choices=[True, False], default=True,
         doc="If C3 fails the close rule, accept C4 closing above C2 open."),
    dict(name="min_body_pct_c23", kind="float", low=0.0, high=0.9,
         default=0.5,
         doc="C2 and C3 must each have a body/range ratio above this "
             "(0.5 = more than half-body candle)."),
    dict(name="setup_mode", kind="categorical",
         choices=["OCL", "SELL_FAILURE_BOTTOM", "SELL_FAILURE_TOP"],
         default="OCL",
         doc="Blueprint: OCL (classic buy), SELL_FAILURE_BOTTOM (buy the "
             "failed short on a retest from above), SELL_FAILURE_TOP (sell "
             "the failed long on a retest from below). Run each separately - "
             "each yields its own study, params and report."),

    # --- confirmation gate (the momentum filter) -------------------------- #
    dict(name="roc_period", kind="int", low=3, high=30, default=10,
         doc="Bars used for the rate-of-change in the confirmation gate. "
             "Short = jumpy, long = late."),
    dict(name="z_window", kind="int", low=20, high=250, default=100,
         doc="Rolling window used to z-score that ROC. This is the parameter "
             "that decides what 'unusual momentum' means."),
    dict(name="entry_z", kind="float", low=0.2, high=3.0, default=1.0,
         doc="How many sigma of z-scored ROC the confirmation requires. This "
             "is the main precision/recall dial of the whole strategy."),

    # --- execution band (how the child orders are priced) ----------------- #
    dict(name="band_ceiling_pct", kind="float", low=0.10, high=1.50,
         default=0.50,
         doc="Hard ceiling on the execution band above VWAP, in percent. The "
             "slicer halts rather than pay more than this."),

    # --- fire behaviour --------------------------------------------------- #
    dict(name="exec_mode", kind="categorical",
         choices=["ONE_SHOT", "CONTINUOUS", "COUNT_LIMITED", "AUTO"],
         default="ONE_SHOT",
         doc="How many times one pattern may fire. Must match the values the "
             "signal's own constructor validates (see ocl_keylevel.py:109)."),
    dict(name="cooldown_seconds", kind="float", low=0.0, high=1800.0,
         default=0.0,
         doc="Minimum gap between consecutive fires. Only meaningful for "
             "CONTINUOUS / COUNT_LIMITED."),

    # --- exit management (stops / breakeven) ------------------------------- #
    dict(name="target_rr", kind="float", low=0.5, high=10.0, default=3.0,
         doc="Fixed risk-reward target (entry + risk * target_rr). Used when "
             "exit_mode resolves to FIXED."),
    dict(name="exit_mode", kind="categorical",
         choices=["AUTO", "FIXED", "DYNAMIC"], default="AUTO",
         doc="AUTO: aggressive entry -> FIXED target, conservative entry -> "
             "ATR trail. FIXED/DYNAMIC force one behaviour."),
    dict(name="breakeven_rr", kind="float", low=0.5, high=4.0, default=1.5,
         doc="Price must reach this many R before the stop moves to entry + "
             "round-trip cost. Lower = protects earlier, cuts more winners."),
    dict(name="round_trip_cost_pct", kind="float", low=0.0, high=1.0,
         default=0.20,
         doc="Round-trip cost in percent of entry. Sets the breakeven floor "
             "so a breakeven exit is scratch, not a small loss."),
    dict(name="atr_trail_enabled", kind="categorical", choices=[True, False],
         default=True,
         doc="Enable the ATR trailing stop after entry."),
    dict(name="atr_period", kind="int", low=5, high=50, default=14,
         doc="ATR lookback for the trailing stop."),
    dict(name="atr_multiplier", kind="float", low=0.5, high=6.0, default=2.0,
         doc="Trail distance in ATRs below the highest close since entry."),

    # --- DV-ATR: dynamic volatility smoothing ATR ------------------------- #
    dict(name="dvs_enabled", kind="categorical", choices=[True, False],
         default=True,
         doc="Dynamic-volatility-smoothing ATR: the trail widens when realized "
             "volatility is above its typical level and tightens below it."),
    dict(name="dvs_sensitivity", kind="float", low=0.0, high=3.0, default=1.0,
         doc="How strongly the vol ratio scales the trail. 0 = classic ATR."),
    dict(name="dvs_max_scale", kind="float", low=1.0, high=5.0, default=2.5,
         doc="Clamp on the widening/tightening factor so one vol spike cannot "
             "push the stop arbitrarily far."),
    dict(name="dvs_reference_period", kind="int", low=10, high=300, default=50,
         doc="Lookback that defines the 'typical' volatility the current one "
             "is compared against."),
    dict(name="dvs_atr_sizing_factor", kind="float", low=0.1, high=2.0,
         default=1.0,
         doc="Size multiplier while the DV-ATR owns the stop. Under 1 trims "
             "risk when the adaptive trail is doing the work."),
    dict(name="stop_fallback_atr_mult", kind="float", low=0.2, high=10.0,
         default=2.0,
         doc="Traditional fallback SL = entry - mult * ATR. Always sits below "
             "the DV-ATR trail, so it is the wide safety net, not the exit."),
]


def _ocl_constraints() -> List[Any]:
    """Cross-parameter rules for the OCL space.

    Each returns a *string reason* when the combination is illegal, or None.
    """

    def cooldown_only_when_multi_fire(p: Dict[str, Any]) -> Optional[str]:
        # cooldown_seconds is ignored by ONE_SHOT (the signal says so at
        # ocl_keylevel.py:136). Searching it under ONE_SHOT would produce a
        # parameter that looks irrelevant and would then be ranked as
        # "not driving the ship" - a false finding caused by the search, not
        # by the market. Refusing the combination keeps importance honest.
        mode = str(p.get("exec_mode", "")).upper()
        if mode == "ONE_SHOT" and float(p.get("cooldown_seconds") or 0) > 0:
            return "cooldown_seconds has no effect under exec_mode=ONE_SHOT"
        return None

    def z_window_long_enough_for_roc(p: Dict[str, Any]) -> Optional[str]:
        # A z-score window shorter than the ROC window cannot be computed at
        # the start of the series: z_window must be >= roc_period or the first
        # roc_period rows of every z-score are undefined.
        if int(p.get("z_window") or 0) < int(p.get("roc_period") or 0):
            return "z_window must be >= roc_period"
        return None

    def dvs_reference_longer_than_atr(p: Dict[str, Any]) -> Optional[str]:
        # The reference window must be longer than the ATR window, otherwise
        # the 'typical volatility' sample is built from the same bars it is
        # supposed to benchmark and the scaling collapses to ~1.
        if not p.get("dvs_enabled", True):
            return None
        if int(p.get("dvs_reference_period") or 0) <= int(p.get("atr_period") or 0):
            return "dvs_reference_period must be > atr_period"
        return None

    def failure_top_is_a_sell_blueprint(p: Dict[str, Any]) -> Optional[str]:
        # SELL_FAILURE_TOP is the only SELL blueprint in this family. Anything
        # that assumes a BUY-only keylevel strategy is meaningless there, so
        # the combination is refused rather than silently evaluated on a
        # configuration the signal will reject.
        if str(p.get("setup_mode")) == "SELL_FAILURE_TOP" and str(
                p.get("trend_filter", "")).upper() == "BULL_ONLY":
            return "trend_filter=BULL_ONLY is incompatible with SELL_FAILURE_TOP"
        return None

    def sell_failure_ignores_touch_buffer(p: Dict[str, Any]) -> Optional[str]:
        # In sell-failure mode the entry is a RETEST of the broken level,
        # so the extra touch buffer would widen a level that the market has
        # already proven. Left in the space it would be a dead axis.
        if p.get("sell_failure_enabled") and float(
                p.get("ltf_touch_buffer") or 0) > 0.05:
            return "ltf_touch_buffer must be <= 0.05 under sell_failure_enabled"
        return None

    def body_filter_needs_half_candle(p: Dict[str, Any]) -> Optional[str]:
        # A body threshold at/above 0.9 rejects almost every real candle and
        # shrinks the candidate set to noise; the blueprint says >50%.
        if float(p.get("min_body_pct_c23") or 0) >= 0.9:
            return "min_body_pct_c23 must be < 0.9"
        return None

    def breakeven_below_target(p: Dict[str, Any]) -> Optional[str]:
        # A breakeven trigger above the fixed target can never arm before
        # the trade is already closed - a structural dead axis, not a real
        # finding.
        if str(p.get("exit_mode", "AUTO")).upper() == "DYNAMIC":
            return None
        if float(p.get("breakeven_rr") or 0) > float(p.get("target_rr") or 0):
            return "breakeven_rr must be <= target_rr"
        return None

    return [cooldown_only_when_multi_fire, z_window_long_enough_for_roc,
            dvs_reference_longer_than_atr, breakeven_below_target,
            failure_top_is_a_sell_blueprint,
            sell_failure_ignores_touch_buffer, body_filter_needs_half_candle]


# --------------------------------------------------------------------------- #
# POV slicer - the execution layer's knobs
# --------------------------------------------------------------------------- #

_POV_SLICER: List[Dict[str, Any]] = [
    dict(name="rate", kind="float", low=0.02, high=0.50, default=0.10,
         doc="Target participation rate as a fraction of observed volume. "
             "0.10 = 10% of the tape."),
    dict(name="min_slice_qty", kind="int", low=1, high=500, default=1,
         doc="Smallest child order the slicer will send."),
    dict(name="max_slice_qty", kind="int", low=10, high=50000, default=1000,
         doc="Largest child order. Caps the market-impact of one child."),
    dict(name="poll_seconds", kind="int", low=1, high=60, default=10,
         doc="How often the slicer re-reads the tape and re-sizes."),
    dict(name="band_pct", kind="float", low=0.01, high=1.50, default=0.10,
         doc="Half-width of the allowed price band around VWAP, in percent."),
    dict(name="ceiling_pct", kind="float", low=0.10, high=2.00, default=0.50,
         doc="Absolute cap above VWAP before slicing halts."),
]


def _pov_constraints() -> List[Any]:
    def qty_band_ordered(p: Dict[str, Any]) -> Optional[str]:
        if int(p.get("min_slice_qty") or 0) > int(p.get("max_slice_qty") or 0):
            return "min_slice_qty must be <= max_slice_qty"
        return None

    def band_inside_ceiling(p: Dict[str, Any]) -> Optional[str]:
        # A band wider than the ceiling can never be reached: the slicer halts
        # at the ceiling first, so band_pct becomes a dead parameter.
        if float(p.get("band_pct") or 0) > float(p.get("ceiling_pct") or 0):
            return "band_pct must be <= ceiling_pct"
        return None

    return [qty_band_ordered, band_inside_ceiling]


# --------------------------------------------------------------------------- #
# Portfolio rebalance - the lever that actually moves P&L
# --------------------------------------------------------------------------- #

_PORTFOLIO_REBALANCE: List[Dict[str, Any]] = [
    dict(name="step", kind="int", low=5, high=120, default=20,
         doc="Trading days between rebalances. The single highest-impact knob "
             "in portfolio/optim/benchmark.py."),
    dict(name="window", kind="int", low=60, high=500, default=120,
         doc="Days of history the covariance is estimated from."),
    dict(name="band_pct", kind="float", low=0.0, high=10.0, default=0.0,
         doc="No-trade band: a sleeve is only traded if its weight moved more "
             "than this percent. Higher band = fewer, cost-justified trades."),
    dict(name="optimiser", kind="categorical",
         choices=["inverse_vol", "risk_parity", "hrp", "min_variance",
                  "max_sharpe"],
         default="risk_parity",
         doc="Which allocator solves the weights. benchmark.py measured only "
             "~0.04 Sharpe between these, versus ~310 bps of cost between "
             "rebalance policies - so treat this as the secondary knob."),
]


def _portfolio_constraints() -> List[Any]:
    def window_longer_than_step(p: Dict[str, Any]) -> Optional[str]:
        # Estimating a 120-day covariance and rebalancing every 200 days wastes
        # the second half of every window: the estimate is stale by the time it
        # is used, which is a silent performance drag rather than a crash.
        if int(p.get("window") or 0) < int(p.get("step") or 0):
            return "window must be >= step (a stale covariance is not an estimate)"
        return None

    return [window_longer_than_step]


# --------------------------------------------------------------------------- #
# Synthetic demo space - known optimum, used by the tests
# --------------------------------------------------------------------------- #

#: The known optimum of ``strategies.synthetic_band_strategy``. Exposed as a
#: constant so the test asserts against ONE source of truth rather than a copy.
#:
#: CALIBRATION NOTE (this is load-bearing, not decoration)
#: ------------------------------------------------------
#: These values were **measured, not asserted** - re-measured under audit
#: finding C1c against the REAL ``Objective`` class (not a hand-rolled copy), so
#: the numbers match what the engine actually scores.
#:
#: Measurement protocol (19,068 evaluations, 4 staged passes):
#:     metric  = mean(fold_sharpe) - 0.5 * std(fold_sharpe), activity-weighted
#:     folds   = 4 x 150 bars, seeds 7 / 1007 / 2007 / 3007, 10 bps round-trip
#:     result  = score 3.8360, mean_sharpe 4.1948, std 0.7177, min_fold 3.3281,
#:               all 4 folds ACTIVE, n_obs 596
#:
#: Traps found by probing, recorded so they are not re-introduced:
#:
#: 1. ``threshold`` gates the *normalised MA gap* ``(fast-slow)/slow``, whose
#:    achievable range on the synthetic series is roughly +/-0.06. An earlier
#:    threshold of 0.35 gated EVERY bar to zero, so the strategy traded nothing
#:    and the fixture had no peak for the optimiser to find at all.
#: 2. The previous value here (``fast=8, slow=16``) was WRONG. It scores 2.8962
#:    - about 25% below the true peak - because it was measured on a stale
#:    grid, before the objective was corrected. ``fast=4, slow=18`` scores
#:    3.8360. The old constant was the reason a correct optimiser appeared not
#:    to converge: it was being asked to find a peak that is not there.
#: 3. ``hold_bars`` is DEAD in this fixture and must NOT be used in any
#:    convergence assertion. The fixture's signals are contiguous (every bar
#:    from ``slow`` onward emits a non-zero signal, gap = 1), so the hold
#:    counter is refreshed before it can expire and all 30 values of
#:    ``hold_bars`` produce a bit-identical score. It is a false-importance
#:    parameter: the importance plot will rank it as noise, correctly, and that
#:    is a property of the fixture rather than a bug in the ranking.
#: 4. The peak is a SPIKE, not a plateau: only 1 of 2,988 (fast, slow) pairs
#:    comes within 1% of the top score, and 8 within 5%. Any convergence test
#:    must therefore use a tolerance on the SCORE, not an exact parameter match,
#:    or it will fail on a run that converged perfectly well.
#:
#: The *shape* the test relies on: a decisive penalty for the wrong ``mode``
#: (meanrev scored about -4.6 against +3.8 for trend on the same probe). That
#: single categorical is what makes convergence assertable.
SYNTHETIC_OPTIMUM: Dict[str, Any] = {
    "fast": 4,
    "slow": 18,
    "threshold": 0.000,
    "hold_bars": 1,
    "vol_target": 0.40,
    "mode": "trend",
}

#: The measured score of :data:`SYNTHETIC_OPTIMUM` under the default objective
#: (4 folds x 150 bars, seed 7, 10 bps). A convergence test compares against THIS
#: rather than against the parameter values, because the peak is a spike (see
#: trap 4 above) and a run that lands next door is still a successful search.
SYNTHETIC_OPTIMUM_SCORE: float = 3.8360

_SYNTHETIC_DEMO: List[Dict[str, Any]] = [
    dict(name="fast", kind="int", low=2, high=30, default=8,
         doc="Fast moving average, in bars."),
    dict(name="slow", kind="int", low=10, high=120, default=21,
         doc="Slow moving average, in bars."),
    dict(name="threshold", kind="float", low=0.000, high=0.030, default=0.004,
         doc="Minimum normalised MA gap |(fast-slow)/slow| required to take a "
             "position. Bounds match the achievable range of the synthetic "
             "series (~+/-0.06); see SYNTHETIC_OPTIMUM for why."),
    dict(name="hold_bars", kind="int", low=1, high=30, default=10,
         doc="Bars the position is held after entry."),
    dict(name="vol_target", kind="float", low=0.05, high=0.40, default=0.12,
         doc="Annualised volatility target used for position sizing."),
    dict(name="mode", kind="categorical", choices=["trend", "meanrev"],
         default="trend", doc="Signal family. Categorical on purpose: the "
                              "importance plot must handle a non-numeric axis."),
]


def _synthetic_constraints() -> List[Any]:
    def fast_before_slow(p: Dict[str, Any]) -> Optional[str]:
        # The textbook crossover rule. fast == slow is a zero-length crossover
        # (no signal at all), not a neutral setting.
        if int(p.get("fast") or 0) >= int(p.get("slow") or 0):
            return "fast must be < slow"
        return None

    return [fast_before_slow]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

_BUILTINS: Dict[str, SearchSpace] = {
    "ocl_keylevel": SearchSpace(
        name="ocl_keylevel",
        params=[ParamSpec(**d) for d in _OCL_KEYLEVEL],
        description="OCL keylevel confirmation strategy - pattern, touch, "
                    "momentum-confirmation and fire behaviour (10 parameters).",
        constraints=_ocl_constraints(),
        meta={"strategy": "blue_engine.alpha.Statergy.ocl_keylevel.OCLKeylevelSignal",
              "source_of_defaults": "alpha/Statergy/ocl_keylevel.py"},
    ),
    "pov_slicer": SearchSpace(
        name="pov_slicer",
        params=[ParamSpec(**d) for d in _POV_SLICER],
        description="POV execution slicer - participation rate, child sizes and "
                    "the price band (6 parameters).",
        constraints=_pov_constraints(),
        meta={"strategy": "blue_engine.pov.slicers.pov.POVSlicer"},
    ),
    "portfolio_rebalance": SearchSpace(
        name="portfolio_rebalance",
        params=[ParamSpec(**d) for d in _PORTFOLIO_REBALANCE],
        description="Portfolio rebalance policy - frequency, covariance window, "
                    "no-trade band and optimiser (4 parameters).",
        constraints=_portfolio_constraints(),
        meta={"strategy": "blue_engine.portfolio.optim.benchmark"},
    ),
    "synthetic_demo": SearchSpace(
        name="synthetic_demo",
        params=[ParamSpec(**d) for d in _SYNTHETIC_DEMO],
        description="A space whose true optimum is known. Used to verify the "
                    "optimiser itself, and as a no-broker demo.",
        constraints=_synthetic_constraints(),
        meta={"strategy": "blue_engine.optimizer.strategies.synthetic_band_strategy",
              "known_optimum": dict(SYNTHETIC_OPTIMUM)},
    ),
}

#: Live registry. ``register_space`` mutates this so a desk can add a space at
#: runtime (e.g. from a research script) without editing this file.
SPACES: Dict[str, SearchSpace] = dict(_BUILTINS)


def register_space(space: SearchSpace, *, replace: bool = True) -> None:
    """Add or replace a space in the process-wide registry."""
    if not replace and space.name in SPACES:
        raise KeyError(f"space {space.name!r} already registered")
    SPACES[space.name] = space
    logger.debug("registered optimizer space %s (%d params)",
                 space.name, space.n_params)


def available_spaces() -> List[str]:
    """Names of every registered space, sorted (stable for CLI `--list`)."""
    return sorted(SPACES)


def builtin_space(name: str) -> SearchSpace:
    """Return the shipped space called ``name`` (``KeyError`` if unknown)."""
    if name not in _BUILTINS:
        raise KeyError(f"unknown built-in space {name!r}; "
                       f"available: {sorted(_BUILTINS)}")
    return _BUILTINS[name]


def _override_path() -> str:
    from . import config
    return os.path.join(config.DATA_ROOT, "optimizer", "param_space.json")


def load_space(name: str, path: Optional[str] = None) -> SearchSpace:
    """Resolve a space by name, honouring the JSON override.

    Resolution order:
        1. an explicit ``path`` (a JSON file containing one space definition),
        2. ``data/optimizer/param_space.json`` entry called ``name``,
        3. the shipped built-in.

    The override is validated by the same constructors as the built-ins, so a
    malformed file fails loudly here instead of producing a strange search.
    """
    p = path or _override_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except Exception as exc:  # noqa: BLE001 - a bad override must be visible
            logger.warning("could not read optimizer space override %s: %s", p, exc)
            blob = None
        if isinstance(blob, dict):
            entry = blob.get(name)
            if isinstance(entry, dict) and entry.get("params"):
                space = make_space(
                    name=name,
                    definition=entry["params"],
                    description=str(entry.get("description", "override")),
                    meta={"source": p},
                )
                logger.info("using optimizer space override for %s from %s",
                            name, p)
                return space
    if name in SPACES:
        return SPACES[name]
    return builtin_space(name)
