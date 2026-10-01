"""
Blue_engine.optimizer - strategy parameter search, done honestly.

WHAT THIS PACKAGE ANSWERS (the only question it exists for)
-----------------------------------------------------------
"Given one strategy and the search budget I can afford, which parameter
setting should I actually trade?"

It answers with an out-of-sample Sharpe, *deflated for the fact that we
searched*, plus the three visuals an operator needs to judge the answer:

    1. which parameters are driving the ship       -> plots.plot_param_importances
    2. each important parameter's sweet spot       -> plots.plot_slice
    3. how those parameters weave together         -> plots.plot_parallel_coordinate

WHY NOT JUST SWEEP EVERY COMBINATION
------------------------------------
Blue_engine/portfolio/optim/benchmark.py already does an exhaustive *grid* over
two rebalance knobs. That is fine for two knobs with 3 values each (9 runs). It
is hopeless for a 10-parameter strategy: even 3 values per knob is 59,049 runs.
So this package uses **TPE** (Tree-structured Parzen Estimator, Bergstra et al.
2011): it models "which parameter regions produced good trials" and samples
there, instead of walking the grid in order.

HARD RULES (violating any of these makes the output useless)
------------------------------------------------------------
1. LEAK-SAFE. Every trial is scored on walk-forward folds from
   ``ml/evaluation/splits.py``. A trial never fits on the block it is scored on.
   The engine reuses that module rather than re-implementing fold geometry.
2. DEFLATED. Searching N trials *is* a selection effect - the maximum of N
   noisy draws is biased upward. The best trial's Sharpe is reported through
   ``metrics.deflated_sharpe(observed, n_trials=N)``. See Bailey & Lopez de
   Prado, "The Deflated Sharpe Ratio" (2014).
3. COST-AWARE. Net of Indian transaction costs, always. An edge that does not
   survive ~10 bps round-trip delivery cost does not exist.
4. DEGRADE, NEVER HALT. Optuna is NOT a dependency (it is not installed in this
   project's venv). If it is present we use its TPE sampler; if not we use the
   built-in fallback in ``sampler.py``. Neither is imported at package import
   time, so importing this package is always safe on a bare machine.
5. READ-ONLY W.R.T. CAPITAL. Nothing in this package places an order, promotes
   a config, or writes to the live parameter file. The output is a
   recommendation; adopting it is a separate, deliberate human action.

LAYOUT
------
    search_space.py   typed, bounded parameter definitions + sampling/validation
    spaces.py         ready-made spaces (OCL keylevel 10-param, POV, rebalance)
    strategies.py     what actually gets called per trial (adapter + benchmark)
    objective.py      param dict -> out-of-sample Sharpe (the honest objective)
    sampler.py        TPE fallback when Optuna is absent
    study.py          trial journal, pruning, parameter importance
    engine.py         the search loop, reproducibility, budget guard
    analysis.py       importance / top-k selection / the 3-step workflow
    plots.py          the three Plotly figures
    report.py         JSON + Markdown deliverables
    store.py          crash-safe JSONL persistence under data/optimizer/

Every module logs through ``blue_engine.optimizer.<module>`` so one dashboard
page can tail all optimizer activity, exactly like ``blue_engine.ml``.
"""
from __future__ import annotations

import logging
import os

# Stable namespace so dashboard log filters survive file moves.
logger = logging.getLogger("blue_engine.optimizer")

__all__ = ["logger", "__version__", "data_root", "config"]

#: Bump when the *objective definition* changes. A study run under an older
#: objective version is not comparable with a newer one, because the number on
#: the y-axis means a different thing. Keeping the version on the study record
#: is what stops two incomparable runs being plotted on one chart.
__version__ = "0.1.0"


def _default_data_root() -> str:
    """Return the repository ``data/`` directory, resolved from *this file*.

    Computing the path relative to the module (not the process CWD) matters
    because the CLI, a job and the dashboard all start from different folders.
    A relative path would silently point somewhere else in each case, and the
    "my studies disappeared" bug would be unreproducible.
    """
    here = os.path.dirname(os.path.abspath(__file__))            # .../Blue_engine/optimizer
    repo_root = os.path.dirname(os.path.dirname(here))           # .../my-trading-system
    return os.path.join(repo_root, "data")


def data_root() -> str:
    """Root for optimizer studies/reports. Override with ``BLUE_OPTIMIZER_DATA_ROOT``."""
    return os.environ.get("BLUE_OPTIMIZER_DATA_ROOT", _default_data_root())


class _ConfigMeta(type):
    """Resolve the directory constants on every read.

    WHY: if these were computed once at import, setting
    ``BLUE_OPTIMIZER_DATA_ROOT`` later in the same process (a test harness, a
    dashboard launched with an env block, a job that switches roots between
    runs) would have no effect - reads and writes would keep hitting the
    default root. Nothing would error; the files would simply not be where the
    caller looked. Resolving on read makes the env var authoritative.
    """

    _DERIVED = {
        "DATA_ROOT": "",
        "STUDIES_DIR": "optimizer_studies",
        "REPORTS_DIR": "optimizer_reports",
    }

    def __getattr__(cls, item: str):
        if item in cls._DERIVED:  # directory constants derived from the root
            sub = cls._DERIVED[item]
            return os.path.join(data_root(), sub) if sub else data_root()
        raise AttributeError(item)


class config(metaclass=_ConfigMeta):  # noqa: N801 - reads as a namespace
    """Plain constants for the optimizer layer.

    Anything that must vary per run belongs in the study record, not here.
    """

    #: Indian round-trip delivery cost assumption, bps. Matches
    #: ``input/macro_master.json`` and ``ml/evaluation/walk_forward``'s default.
    COST_BPS_ROUND_TRIP = 10.0

    #: How many trials before we tell the operator the search is still too thin
    #: to trust the importance ranking. Importance from 12 trials is noise; the
    #: literature (and Optuna's own guidance) wants at least ~50 for a stable
    #: fANOVA-style ranking. We do not block the run - we mark the output
    #: "under-budget" so a small study cannot masquerade as a finished one.
    MIN_TRIALS_FOR_IMPORTANCE = 50

    #: Fractions of trials that must reach the top decile before a parameter is
    #: treated as "driving the ship" when importance cannot be computed.
    TOP_DECILE_FRACTION = 0.10

    #: Default number of important parameters carried into the slice and
    #: parallel-coordinate steps. AGENTS.md says "which 3 or 4 parameters are
    #: driving the ship" - four is the top of that range.
    DEFAULT_TOP_K = 4


def ensure_dirs() -> dict:
    """Create the optimizer data directories. Returns the paths created.

    Idempotent. Called by the CLI/report writers rather than at import time so
    that importing this package has no filesystem side effects.
    """
    paths = {
        "root": config.DATA_ROOT,
        "studies": config.STUDIES_DIR,
        "reports": config.REPORTS_DIR,
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    return paths
