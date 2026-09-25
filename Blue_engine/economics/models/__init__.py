"""
models - pure statistical / ML models for the economic engine.

No I/O. Every module degrades to a safe no-op when its optional dependency is
missing, so the engine never crashes for want of a library.

    hmm_regime     Gaussian HMM -> P(regime) posterior (numpy-only impl)
    cluster        k-means macro states (numpy-only impl)
    classifier     macro->market classifier with walk-forward CV (sklearn opt.)
    dl_nowcast     deep-learning nowcast interface (torch opt., stub)
    regime_blend   combine model posteriors into one exposure multiplier
"""
from __future__ import annotations

from . import classifier, cluster, dl_nowcast, hmm_regime
from .hmm_regime import GaussianHMM, detect_regime

__all__ = [
    "hmm_regime", "detect_regime", "GaussianHMM",
    "cluster", "classifier", "dl_nowcast",
]