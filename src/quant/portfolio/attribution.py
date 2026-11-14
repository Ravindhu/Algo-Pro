"""Performance attribution wrapper: Brinson and factor decomposition, DELEGATED to Blue_engine.

Flow stage: Performance attribution
Status: PHASE 5 IMPLEMENTED (wrap; pure statistics; no orders)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/portfolio/attribution/brinson.py,
src/quant/blue_engine/portfolio/attribution/factor_attr.py

Sharpe is deliberately NOT wrapped here. Blue has two Sharpe definitions and they
disagree on the same window (audit X-27). The owner must pick one before any report
shows a Sharpe number. Our rolling Sharpe in monitor/strategy_health stays the only one.

FAIL-CLOSED RULES
-----------------
- Blue unavailable or a computation raises: returns None.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["brinson", "active_return", "factor_returns", "factor_risk"]


def brinson(weights: Dict[str, float], bench_weights: Dict[str, float],
            sleeve_returns: Dict[str, float],
            bench_sleeve_returns: Dict[str, float]) -> Optional[Dict[str, Any]]:
    try:
        fn = load("portfolio.attribution.brinson").brinson_attribution
    except BlueEngineUnavailable:
        return None
    try:
        out = fn(weights, bench_weights, sleeve_returns, bench_sleeve_returns)
    except Exception:  # noqa: BLE001
        return None
    return dict(out) if isinstance(out, dict) else None


def active_return(weights: Dict[str, float], sleeve_returns: Dict[str, float],
                  bench_weights: Dict[str, float],
                  bench_sleeve_returns: Dict[str, float]) -> Optional[float]:
    try:
        fn = load("portfolio.attribution.brinson").active_return
    except BlueEngineUnavailable:
        return None
    try:
        out = fn(weights, sleeve_returns, bench_weights, bench_sleeve_returns)
    except Exception:  # noqa: BLE001
        return None
    return None if out is None else float(out)


def factor_returns(portfolio_returns: Sequence[float], factor_returns_: Any,
                   factor_names: Sequence[str]) -> Optional[Dict[str, Any]]:
    try:
        fn = load("portfolio.attribution.factor_attr").return_attribution
    except BlueEngineUnavailable:
        return None
    try:
        out = fn(portfolio_returns, factor_returns_, factor_names)
    except Exception:  # noqa: BLE001
        return None
    return dict(out) if isinstance(out, dict) else None


def factor_risk(weights: Any, sleeve_factor_betas: Any, factor_cov: Any,
                factor_names: Sequence[str], idiosyncratic_var: Any) -> Optional[Dict[str, Any]]:
    try:
        fn = load("portfolio.attribution.factor_attr").risk_attribution
    except BlueEngineUnavailable:
        return None
    try:
        out = fn(weights, sleeve_factor_betas, factor_cov, factor_names, idiosyncratic_var)
    except Exception:  # noqa: BLE001
        return None
    return dict(out) if isinstance(out, dict) else None
