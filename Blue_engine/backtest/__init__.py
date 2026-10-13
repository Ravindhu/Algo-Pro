"""
blue_engine.backtest - the Validation-Mode backtesting engine.

WHAT THIS PACKAGE IS (and what it deliberately is NOT)
======================================================
AGENTS.md and the Blue_engine architecture split the trading system into three
engines with three different jobs:

    Backtesting Engine      -> "is this parameter set real?"   (VALIDATION)
    Optimization Engine     -> "which parameter set is best?"   (SEARCH)
    Live Execution Engine   -> "put the order on the wire"      (EXECUTION)

The **Optimization** engine already exists in ``blue_engine.optimizer`` (TPE
sampler, walk-forward objective, crash-safe study journal). The **Live
Execution** engine already exists as ``blue_engine.core`` + ``blue_engine.pov``
(the runner, the slicer, the ledger, the risk gate).

So this package is NOT "another backtester". A second backtester that scores a
parameter set with its own metrics would be the exact duplication AGENTS.md
forbids, and worse, the two would silently disagree - one number for the search,
another for the report.

This package is the **single deterministic event-driven simulator**. The
optimizer's strategy adapter calls this same engine, and the symbol-batch flow
uses its result for validation. One implementation, one place to audit, one
definition of "net of Indian costs".

THE THREE FEEDBACK LOOPS (AGENTS.md "Backtesting / Optimization / Live")
========================================================================
Loop A - IS -> OOS validation. ``batch.run_symbol_batch``. After a study, the winner is
    re-run on data the search never touched. The gate is explicit and published:
        Sharpe_OOS >= 0.7 * Sharpe_IS   AND   MaxDD_OOS <= 1.3 * MaxDD_IS
    A parameter set that fails stays un-promoted. This is the loop that stops a
    curve fit from reaching production.

Loop B - performance monitoring -> re-optimisation trigger.
    ``loops.LiveMonitor``. Given the realised return/equity stream and the
    historical envelope the study recorded, it raises a re-optimisation signal
    when ANY of the published conditions trips:
        live drawdown      > 1.5 * historical max drawdown
        losing streak      > 1.4 * historical max losing streak
        regime shift       > 2 std dev of 10-day rolling volatility vs history
    It returns a reason-bearing signal with an entry-pause request. The live
    orchestrator must consume that signal; this module cannot place orders.

Loop C - structural reconciliation (simulated vs live slippage).
    ``loops.SlippageReconciler``. It consumes explicitly matched long-entry
    fills, calibrates a copied cost model from the average relative price
    delta, and can immediately re-run the same backtest.

HARD RULES
==========
1. DETERMINISTIC. Same data + same params + same cost config => same numbers.
   No wall-clock, no unordered iteration, no hidden global RNG. A backtest whose
   output changes between runs cannot be used to promote a parameter set.
2. NO LOOK-AHEAD. A decision on bar ``t`` may only use bars ``<= t``. The engine
   enforces this structurally: the strategy is handed a prefix view, and the
   fill price is always the *next* bar's open (plus slippage), never the close
   of the bar the decision was made on.
3. COSTS ARE FIRST-CLASS. Every fill is charged the full Indian stack - brokerage,
   STT, exchange txn, GST, SEBI, stamp duty - plus modelled slippage and a
   latency penalty. Reused from ``portfolio.optim.cost_aware`` so the planner and
   the simulator cannot disagree.
4. ZERO NETWORK DURING SIMULATION. ``run_backtest`` never calls OpenAlgo. The
   batch's separate preparation stage may download missing history into the
   existing local SQLite cache; parameter trials and OOS runs only read cached
   rows. Synthetic data is never silently substituted for missing history.
5. REUSE, DO NOT DUPLICATE. Metrics come from ``ml.evaluation.metrics``. Cost
   rates come from ``portfolio.optim.cost_aware``. Fold geometry comes from
   ``ml.evaluation.splits``. This package adds the event loop that was missing.

LAYOUT
======
    costs.py        the cost/slippage model (wraps cost_aware + adds slippage,
                    latency and the market-impact term)
    bars.py         bar-series adapter: SQLite rows -> a typed, validated series
    engine.py       the deterministic event loop + BacktestResult
    metrics.py      the report block (Sharpe, Sortino, Calmar, expectancy, ...)
                    computed with ml.evaluation.metrics wherever it exists
    strategy.py     the optimizer strategy contract that wraps the engine
    loops.py        Loop A (IS/OOS), Loop B (live monitor), Loop C (slippage)
    batch.py        cached symbol ingestion, parallel search, OOS gate and JSON output

Every module logs through ``blue_engine.backtest.<module>`` so one dashboard
page can tail all backtest activity, exactly like ``blue_engine.optimizer``.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("blue_engine.backtest")

__all__ = ["logger", "__version__", "config", "data_root", "ensure_dirs"]

#: Bump when the *cost model or the fill convention* changes. A result produced
#: under an older version is not comparable with a newer one, because the same
#: parameter set would be charged a different amount. Keeping the version on the
#: result is what stops two incomparable runs being compared on one dashboard.
__version__ = "0.1.0"


def _default_data_root() -> str:
    """The repository ``data/`` directory, resolved from *this file*.

    Resolving relative to the module (not the process CWD) matters because the
    CLI, a background worker and the dashboard all start from different folders.
    A relative path would silently point somewhere else in each case and the
    "my backtests disappeared" bug would be unreproducible.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    return os.path.join(repo_root, "data")


def data_root() -> str:
    """Root for backtest artefacts. Override with ``BLUE_BACKTEST_DATA_ROOT``."""
    return os.environ.get("BLUE_BACKTEST_DATA_ROOT", _default_data_root())


class _ConfigMeta(type):
    """Resolve directory constants on every read (env var stays authoritative)."""

    _DERIVED = {
        "DATA_ROOT": "",
        "RESULTS_DIR": "backtest_results",
        "RECON_DIR": "backtest_reconciliation",
    }

    def __getattr__(cls, item: str):
        if item in cls._DERIVED:
            sub = cls._DERIVED[item]
            return os.path.join(data_root(), sub) if sub else data_root()
        raise AttributeError(item)


class config(metaclass=_ConfigMeta):  # noqa: N801 - reads as a namespace
    """Plain constants for the backtest layer. Per-run values live on the result."""

    #: Indian delivery round-trip cost, bps. Kept in sync with
    #: ``optimizer.config.COST_BPS_ROUND_TRIP`` and ``input/macro_master.json``
    #: so a study, a backtest and a report all charge the same amount.
    COST_BPS_ROUND_TRIP = 10.0

    #: Default modelled slippage, bps of traded notional, ON TOP of the explicit
    #: cost stack. Calibrated upward by Loop C from realised live fills. Starting
    #: at 5 bps rather than 0 is deliberate: a zero-slippage backtest is the
    #: single most common way a strategy looks tradeable and then is not.
    DEFAULT_SLIPPAGE_BPS = 5.0

    #: Latency penalty, bps. Represents the gap between the decision bar's close
    #: and the achievable fill: a real order does not get the close it decided on.
    #: Charged as a fixed cost per position change, alongside slippage.
    DEFAULT_LATENCY_BPS = 1.0

    #: Loop A gate thresholds. Published as constants so a report can quote the
    #: bar it applied instead of an unexplained "pass"/"fail".
    #:
    #: WHY THERE ARE TWO SHARPE LEGS AND NOT ONE
    #: -----------------------------------------
    #: ``OOS_SHARPE_RATIO_MIN`` is a RELATIVE floor: the winner must retain a
    #: stated fraction of its IS Sharpe. On its own it is a shrinkage test, not a
    #: validity test - a search that curve-fits IS Sharpe 6.0 clears a 0.7 floor
    #: with an OOS Sharpe of 4.2, and nothing about that number is defensible.
    #: ``OOS_SHARPE_ABS_MIN`` is the ABSOLUTE leg: an economically meaningful net
    #: minimum. Both legs must pass. The absolute figure is a working assumption
    #: for Indian equity delivery AFTER costs, not a measured constant.
    OOS_SHARPE_RATIO_MIN = 0.7
    OOS_SHARPE_ABS_MIN = 1.0
    OOS_DRAWDOWN_RATIO_MAX = 1.3

    #: Family-wise confidence target for the trial-count penalty. A search of N
    #: trials is charged as if we demanded this confidence across the whole
    #: family of trials, so a larger search needs a larger OOS Sharpe to pass.
    #: 0.95 means "at most a 5% chance the winner is the luckiest of N draws".
    OOS_FAMILY_CONFIDENCE = 0.95

    #: Loop B trigger thresholds, expressed as multiples of the historical value.
    LIVE_DRAWDOWN_MULT = 1.5
    LIVE_LOSS_STREAK_MULT = 1.4
    REGIME_VOL_STD_MULT = 2.0
    REGIME_WINDOW_DAYS = 10

    #: Minimum trades for a backtest to be considered scoreable at all. Below
    #: this the Sharpe is an estimate from too few draws to mean anything, and
    #: the engine marks the result "insufficient sample" rather than scoring it.
    MIN_TRADES_FOR_VERDICT = 10


def ensure_dirs() -> dict:
    """Create the backtest artefact directories. Idempotent; no import side effects."""
    paths = {
        "root": config.DATA_ROOT,
        "results": config.RESULTS_DIR,
        "recon": config.RECON_DIR,
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    return paths
