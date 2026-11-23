"""
attribution - performance & risk attribution for the Portfolio engine (pure).

Turning a P&L number into *why* it happened. Four views:

    brinson        allocation vs selection effect (Brinson-Fachler)
    factor_attr    decompose returns into factor exposures
    perf           Sharpe/Sortino/Calmar/IR/tracking error/capture
    drift          target-vs-actual drift time series

All pure: inputs are weight/return/return-series dicts; outputs are numbers and
small tables the dashboard renders directly.
"""
from . import brinson, drift, factor_attr, perf, tearsheet
__all__ = ["brinson", "factor_attr", "perf", "drift", "tearsheet"]