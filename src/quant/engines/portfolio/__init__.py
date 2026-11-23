"""
portfolio - Portfolio engine package.

Orchestrator + pure building blocks for the capital-allocation stage::

    portfolio_engine   PortfolioEngine (orchestrator; pure, no I/O)
    portfolio_state    append-only JSONL run store (drift history)
    providers/         data adapters (the ONLY I/O; SDK read-only + config)
    risk/              covariance, volatility, tail, beta, contribution, factors
    optim/             optimisers (ERC, HRP, NCO, min-var, max-Sharpe, BL, CVaR)
    sizing/            vol-target, fractional Kelly, drawdown governor
    attribution/       Brinson, factor attribution, performance, drift
    views/             ML / NLP / LLM expected-return views for Black-Litterman

Backwards compatible: ``PortfolioEngine`` is re-exported exactly as before.
"""
from . import (attribution, optim, portfolio_state, providers, risk, sizing,
               views)
from .portfolio_engine import PortfolioEngine

__all__ = [
    "PortfolioEngine",
    "providers", "risk", "optim", "sizing", "attribution", "views",
    "portfolio_state",
]