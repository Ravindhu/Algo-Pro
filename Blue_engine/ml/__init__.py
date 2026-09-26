"""
Blue_engine.ml — offline machine-learning layer for the trading system.

WHY THIS PACKAGE EXISTS
-----------------------
The trading engine needs to *improve itself* from data (features -> model ->
probability -> decision), but that learning must NEVER be able to touch a live
order path. This package is therefore deliberately **offline and read-mostly**:

    raw vendor data  ->  immutable point-in-time store  ->  features
    ->  models  ->  validated probabilities  ->  engine policy (separate process)

DESIGN RULES (non-negotiable — see AGENTS.md for the easy-English version)
--------------------------------------------------------------------------
1. NO LOOK-AHEAD. Every training row is built from data that was *knowable at
   that timestamp*. The loader enforces this, not the model. A model that is
   right only because it saw the future is worse than no model at all —
   in a live account it loses real money.
2. NO SILENT FALLBACK. If a data source is missing we record a `Degradation`
   row, the quality gate fails, and the dashboard shows "INSUFFICIENT DATA".
   We never silently train on a shorter/different sample. "Report is not a guess."
3. REPRODUCIBILITY. A model artifact is only valid together with its manifest:
   dataset hash + feature code hash + git commit + params + seed + split dates.
   If the manifest cannot be verified, the artifact must not be loaded live.
4. LEAK-SAFE SPLITS. Time-series walk-forward/purged splits by default. Random
   K-fold on market data leaks and is therefore blocked unless explicitly
   requested with a written reason.
5. OFF-LINE ONLY. Nothing in this package sends an order. Trading modules may
   import *frozen, signed* outputs (probabilities/weights) from the registry,
   never the raw training code path.

WHERE TO PUT THINGS
-------------------
ml/data_sources/  vendor clients (NSE/RBI/OpenAlgo/yfinance/...) — one file per source
ml/features/      feature engineering + the point-in-time join (the leak barrier)
ml/models/        model definitions (sklearn/torch/LightGBM/...) — pure functions/classes
ml/training/      orchestrators that tie source -> features -> split -> fit -> eval
ml/evaluation/    metrics, purged walk-forward, deflated Sharpe, PBO, calibration
ml/registry/      model artifact index (jsonl) + manifest sign/verify
ml/audit/         leak detector, data-quality gate, reports feeding the dashboard

ADDING A NEW DATA SOURCE (the honest 4-step recipe)
---------------------------------------------------
1. Write `ml/data_sources/<vendor>.py` implementing `DataSource.fetch(symbol, start, end)`
   returning a DataFrame whose index is `as_of_ts` (the *knowledge* timestamp),
   not the event timestamp if they differ.
2. Register it in `ml/data_sources/registry.py:SOURCES`.
3. Add a unit test that pins a *known* value from the vendor docs (e.g. RBI
   reference rate on a known date). If the test cannot be written, the source
   is unverified and stays out of production training.
4. Re-run the audit gate: `python Scripts/audit_ml_data_gate.py`.
   Zero rows returned => source marked `degraded`, training refuses to run.

Every module in this package logs through the standard library logger named
`blue_engine.ml.<module>` so one dashboard page can tail all ML activity.
"""

from __future__ import annotations

import logging
import os

# Stable namespace so log filters in the dashboard keep working if files move.
logger = logging.getLogger("blue_engine.ml")

__all__ = ["logger", "__version__", "data_root", "config"]

# The ML layer's version. Bump when a *feature definition* changes: artifacts
# trained under an older feature version are NOT comparable and must be retrained.
__version__ = "0.1.0"


def _default_data_root() -> str:
    """Return the repository `data/` directory, resolved safely.

    We compute this relative to *this file* rather than the process CWD because
    the dashboard, jobs and CLI scripts all start from different folders. A
    relative "data/" would then point somewhere different each time and the
    "missing data" bug would be unreproducible — a classic silent failure.
    """
    here = os.path.dirname(os.path.abspath(__file__))          # .../Blue_engine/ml
    repo_root = os.path.dirname(os.path.dirname(here))          # .../my-trading-system
    return os.path.join(repo_root, "data")


# Root for all ML datasets/artifacts. Override with BLUE_ML_DATA_ROOT for tests.
def data_root() -> str:
    return os.environ.get("BLUE_ML_DATA_ROOT", _default_data_root())


class config:  # noqa: N801 - lowercase intentional: it reads as a namespace, not a class to instantiate
    """Tiny configuration namespace for the offline ML layer.

    Kept as plain constants so that offline scripts import one obvious place.
    Anything that must vary per-run belongs in the run manifest, not here.
    """

    # Datasets
    DATA_ROOT = data_root()
    RAW_DIR = os.path.join(DATA_ROOT, "ml_raw")          # verbatim vendor responses
    CURATED_DIR = os.path.join(DATA_ROOT, "ml_curated")  # leakage-safe, typed tables
    FEATURES_DIR = os.path.join(DATA_ROOT, "ml_features")
    ARTIFACT_DIR = os.path.join(DATA_ROOT, "ml_artifacts")
    REGISTRY_FILE = os.path.join(DATA_ROOT, "ml_artifacts", "registry.jsonl")
    AUDIT_DIR = os.path.join(DATA_ROOT, "ml_audit")

    # Point-in-time contract
    KNOWLEDGE_TS_COL = "as_of_ts"
    #: Minimum history a production model needs. Below this the gate fails rather
    #: than training a model that will be noise. Chosen as 3 years of daily bars —
    #: enough to span one full bull+bear cycle on NIFTY without overfitting a regime.
    MIN_TRAIN_DAYS = 750

    #: Any single trading day with more than this fraction of missing values makes
    #: the whole day suspect (usually a vendor partial outage). Default 20%.
    MAX_MISSING_FRAC = 0.20

    # Safety
    #: Live trading only ever consumes artifacts whose manifest records this status.
    REQUIRED_ARTIFACT_STATUS = "validated"


# Make the module import side-effect-free but loud about misconfiguration.
if not os.path.isdir(config.DATA_ROOT):  # pragma: no cover - environment dependent
    logger.warning(
        "ML data root %s does not exist yet; it will be created on first write.",
        config.DATA_ROOT,
    )
