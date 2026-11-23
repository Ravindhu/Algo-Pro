"""
risk - pure risk-model building blocks for the Portfolio engine.

Everything here is *pure*: no network, no SDK, no disk. Data (returns, weights,
vols, correlations) is passed in; matrices come out. This is what makes the
portfolio engine offline-testable and upgrade-proof.

Modules
-------
covariance    sample / EWMA / Ledoit-Wolf shrinkage covariance estimators
volatility    realised (close-to-close, Parkinson, Garman-Klass), EWMA, HAR-RV
tail          historical & parametric VaR, CVaR (expected shortfall)
beta          OLS / downside beta and factor loadings
contribution  marginal & component risk contribution (risk attribution)
factor_model  PCA eigenvalue cleaning of a covariance matrix

Design rule (from ``AGENTS.md`` and ``core/results.py``): every estimator is
*dependency-optional*. If ``numpy`` is absent, each function degrades to a
pure-stdlib implementation (or returns ``None`` with a warning) rather than
raising. The engine contract is "degrade, never halt".
"""
from . import (beta, contribution, covariance, factor_model, tail, volatility)

__all__ = [
    "covariance", "volatility", "tail", "beta", "contribution", "factor_model",
]