"""
features - pure transforms that turn raw macro readings into model-ready inputs.

None of these modules perform I/O. They are the "feature engineering" layer a
quant would put between the data adapters (:mod:`..providers`) and the models
(:mod:`..models`).

    surprise      value vs expectation (consensus or AR(1))  -> z
    delevel       z-score of change vs rolling baseline
    pca           de-collinearise the macro block (numpy optional)
    stationarity  ADF / KPSS / Granger gates (statsmodels optional)

Every module degrades to a safe no-op when its optional dependency is absent,
so the engine never crashes because a library is missing.
"""
from __future__ import annotations

from . import delevel, pca, stationarity, surprise
from .surprise import ar1_forecast, rolling_sd
from .delevel import z_of_change, delevel_scores
from .surprise import surprise_scores

__all__ = [
    "surprise", "surprise_scores", "ar1_forecast", "rolling_sd",
    "delevel", "pca", "stationarity",
    "z_of_change", "delevel_scores",
]