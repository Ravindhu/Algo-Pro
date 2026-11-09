"""Optimiser registry and regime policy (phases 1-2).

Flow stage: Optimiser (portfolio sizing)
Status: PHASE 2 IMPLEMENTED (stage 6.4 phase 1-2, 2026-11-09: local port; no Blue import)
Ported from: Blue_engine/portfolio/optim/registry.py (names, REGIME_POLICY, policy_for, optimise).

Deliberate difference from Blue: an unknown name (including black_litterman, bl, market_cap,
min_cvar, cvar, which are not ported yet) raises ValueError. Blue silently falls back to
risk_parity. Quant refuses instead (fail closed), so a wrong method is never used by accident.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Type

from quant.portfolio.optimiser_base import BaseOptimiser
from quant.portfolio.optimiser_methods import HRP, NCO, InverseVol, MaxSharpe, MinVariance, RiskParity

__all__ = ["register", "get_optimiser", "available", "policy_for", "optimise", "REGIME_POLICY"]

_REGISTRY: Dict[str, Type[BaseOptimiser]] = {
    "inverse_vol": InverseVol,
    "risk_parity": RiskParity,
    "erc": RiskParity,
    "min_variance": MinVariance,
    "max_sharpe": MaxSharpe,
    "hrp": HRP,
    "nco": NCO,
}


def register(name: str) -> Callable:
    """Decorator: register an optimiser class under ``name``."""

    def deco(cls: Type[BaseOptimiser]) -> Type[BaseOptimiser]:
        _REGISTRY[name.lower()] = cls
        return cls

    return deco


#: Regime -> optimiser name and target vol (same values as Blue).
REGIME_POLICY: Dict[str, Dict[str, Any]] = {
    "RISK_ON":       {"optimiser": "hrp", "target_vol": 0.16},
    "MILD_RISK_ON":  {"optimiser": "hrp", "target_vol": 0.14},
    "NEUTRAL":       {"optimiser": "risk_parity", "target_vol": 0.12},
    "MILD_RISK_OFF": {"optimiser": "min_variance", "target_vol": 0.10},
    "RISK_OFF":      {"optimiser": "min_variance", "target_vol": 0.07},
    "UNKNOWN":       {"optimiser": "risk_parity", "target_vol": 0.12},
}


def get_optimiser(name: str, *, config: Optional[Dict[str, Any]] = None,
                  log: Optional[Callable[[str], None]] = None) -> BaseOptimiser:
    """Instantiate an optimiser by name. Raises ValueError for an unknown name (fail closed)."""
    cls = _REGISTRY.get((name or "").lower())
    if cls is None:
        raise ValueError(f"unknown optimiser '{name}'; available: {available()}")
    return cls(config=config, log=log)


def available() -> List[str]:
    return sorted(_REGISTRY.keys())


def policy_for(regime: Optional[str], *,
               overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Resolve the optimiser and target vol for a regime label."""
    key = (regime or "UNKNOWN").upper()
    policy = dict(REGIME_POLICY.get(key, REGIME_POLICY["UNKNOWN"]))
    if overrides:
        policy.update(overrides.get(key, {}))
    return policy


def optimise(name: str, problem: Dict[str, Any], *, config: Optional[Dict[str, Any]] = None,
             log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """One-call: pick the named optimiser and run it. Never raises (unknown name gives ok=False)."""
    try:
        opt = get_optimiser(name, config=config, log=log)
    except ValueError as exc:
        return {"ok": False, "weights": {}, "method": name, "diagnostics": {},
                "warnings": [str(exc)]}
    return opt.optimise(problem)
