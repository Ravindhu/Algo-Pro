"""
economics - Economic engine package.

Orchestrator + pure building blocks for the macro regime stage::

    economic_engine   EconomicEngine (orchestrator; pure, no I/O)
    scoring           three-view signal blend (level/delevel/surprise)
    regime            banding + gross-exposure multiplier (score & posterior)
    features/         pure transforms (surprise, delevel, pca, stationarity)
    models/           pure models (hmm_regime, cluster, classifier, dl_nowcast)
    providers/        data adapters (the ONLY I/O; SDK + public India sources)
    nlp/              text -> scores (lexical sentiment, LLM, RBI, events)

Backwards compatible: ``EconomicEngine``, ``DEFAULT_SIGNALS`` and ``REGIME_BANDS``
are re-exported exactly as before.
"""
from . import features, models, nlp, providers, regime, run_history, scoring
from .economic_engine import DEFAULT_SIGNALS, REGIME_BANDS, EconomicEngine

__all__ = [
    "EconomicEngine", "DEFAULT_SIGNALS", "REGIME_BANDS",
    "scoring", "regime", "features", "models", "providers", "nlp",
    "run_history",
]