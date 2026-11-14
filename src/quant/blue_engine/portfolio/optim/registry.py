"""
optim/registry.py - name -> optimiser resolution and the ``optimise()`` entry.

Mirrors the plugin style of ``core/registry.py``: optimisers register themselves
by name, and the engine (or a config file) selects one by string. Adding a new
optimiser is a subclass + a register call; no engine edit.

Also hosts the **regime-conditional object selection**: which optimiser and
target-vol the desk should use for each Economic regime. This is the
meta-allocation layer the plan describes.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Type

from .base import BaseOptimiser
from .black_litterman import BlackLitterman, MarketCap
from .cvar import MinCVaR
from .hrp import HRP, NCO
from .mean_variance import MaxSharpe, MinVariance
from .risk_parity import InverseVol, RiskParity

_REGISTRY: Dict[str, Type[BaseOptimiser]] = {}


def register(name: str) -> Callable:
    """Decorator: register an optimiser class under ``name``."""

    def deco(cls: Type[BaseOptimiser]) -> Type[BaseOptimiser]:
        _REGISTRY[name.lower()] = cls
        return cls

    return deco


def _register_builtins() -> None:
    for name, cls in {
        "inverse_vol": InverseVol,
        "risk_parity": RiskParity,
        "erc": RiskParity,             # alias
        "min_variance": MinVariance,
        "max_sharpe": MaxSharpe,
        "hrp": HRP,
        "nco": NCO,
        "black_litterman": BlackLitterman,
        "bl": BlackLitterman,          # alias
        "market_cap": MarketCap,
        "min_cvar": MinCVaR,
        "cvar": MinCVaR,               # alias
    }.items():
        _REGISTRY.setdefault(name, cls)


_register_builtins()

#: Regime -> (optimiser name, target vol). Conservative in risk-off, aggressive
#: in risk-on. This is the meta-allocation policy; override via config.
REGIME_POLICY: Dict[str, Dict[str, Any]] = {
    "RISK_ON":       {"optimiser": "hrp", "target_vol": 0.16},
    "MILD_RISK_ON":  {"optimiser": "hrp", "target_vol": 0.14},
    "NEUTRAL":       {"optimiser": "risk_parity", "target_vol": 0.12},
    "MILD_RISK_OFF": {"optimiser": "min_variance", "target_vol": 0.10},
    "RISK_OFF":      {"optimiser": "min_variance", "target_vol": 0.07},
    "UNKNOWN":       {"optimiser": "risk_parity", "target_vol": 0.12},
}


def get_optimiser(name: str, *, config: Optional[Dict[str, Any]] = None,
                  log: Optional[Callable[[str], None]] = None
                  ) -> BaseOptimiser:
    """Instantiate an optimiser by name (falls back to risk_parity with a log)."""
    cls = _REGISTRY.get((name or "").lower())
    if cls is None:
        if log:
            log(f"[optim] unknown optimiser '{name}'; using risk_parity")
        cls = RiskParity
    return cls(config=config, log=log)


def available() -> List[str]:
    return sorted(_REGISTRY.keys())


def policy_for(regime: Optional[str], *,
               overrides: Optional[Dict[str, Dict[str, Any]]] = None
               ) -> Dict[str, Any]:
    """Resolve the optimiser + target vol for a regime label."""
    policy = dict(REGIME_POLICY.get((regime or "UNKNOWN").upper(),
                                    REGIME_POLICY["UNKNOWN"]))
    if overrides:
        policy.update(overrides.get((regime or "UNKNOWN").upper(), {}))
    return policy


def optimise(name: str, problem: Dict[str, Any], *,
             config: Optional[Dict[str, Any]] = None,
             log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """One-call: pick the named optimiser and run it. Never raises."""
    return get_optimiser(name, config=config, log=log).optimise(problem)