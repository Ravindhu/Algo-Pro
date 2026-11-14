"""
Plugin registry for the 4-engine trading system.

This module provides a decorator-based plugin registration system
that enables auto-discovery of engine implementations at import time.

Usage:
    @register_alpha_engine("momentum")
    class MomentumAlpha(BaseAlphaEngine):
        ...

    @register_risk_engine("conservative")
    class ConservativeRisk(BaseRiskEngine):
        ...

The registry supports plug-in / plug-out via configuration â€” simply
enable/disable engine names in the JSON config to swap implementations
without touching any Python code.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any, Callable, Dict, List, Optional, Type

from .interfaces import BaseAlphaEngine, BaseEMSEngine, BaseOMSEngine, BaseRiskEngine, EngineType

# â”€â”€ Plugin registries â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Each registry maps a *engine name* â†’ engine class.

_alpha_registry: Dict[str, Type[BaseAlphaEngine]] = {}
_risk_registry: Dict[str, Type[BaseRiskEngine]] = {}
_oms_registry: Dict[str, Type[BaseOMSEngine]] = {}
_ems_registry: Dict[str, Type[BaseEMSEngine]] = {}


# â”€â”€ Decorators â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def register_alpha_engine(name: str) -> Callable:
    """Decorator to register an Alpha engine implementation."""

    def decorator(cls: Type[BaseAlphaEngine]) -> Type[BaseAlphaEngine]:
        if not issubclass(cls, BaseAlphaEngine):
            raise TypeError(f"Class {cls.__name__} must inherit from BaseAlphaEngine")
        if inspect.isabstract(cls):
            raise TypeError(f"Class {cls.__name__} is abstract and cannot be registered")
        _alpha_registry[name.lower()] = cls
        return cls

    return decorator


def register_risk_engine(name: str) -> Callable:
    """Decorator to register a Risk engine implementation."""

    def decorator(cls: Type[BaseRiskEngine]) -> Type[BaseRiskEngine]:
        if not issubclass(cls, BaseRiskEngine):
            raise TypeError(f"Class {cls.__name__} must inherit from BaseRiskEngine")
        if inspect.isabstract(cls):
            raise TypeError(f"Class {cls.__name__} is abstract and cannot be registered")
        _risk_registry[name.lower()] = cls
        return cls

    return decorator


def register_oms_engine(name: str) -> Callable:
    """Decorator to register an OMS engine implementation."""

    def decorator(cls: Type[BaseOMSEngine]) -> Type[BaseOMSEngine]:
        if not issubclass(cls, BaseOMSEngine):
            raise TypeError(f"Class {cls.__name__} must inherit from BaseOMSEngine")
        if inspect.isabstract(cls):
            raise TypeError(f"Class {cls.__name__} is abstract and cannot be registered")
        _oms_registry[name.lower()] = cls
        return cls

    return decorator


def register_ems_engine(name: str) -> Callable:
    """Decorator to register an EMS engine implementation."""

    def decorator(cls: Type[BaseEMSEngine]) -> Type[BaseEMSEngine]:
        if not issubclass(cls, BaseEMSEngine):
            raise TypeError(f"Class {cls.__name__} must inherit from BaseEMSEngine")
        if inspect.isabstract(cls):
            raise TypeError(f"Class {cls.__name__} is abstract and cannot be registered")
        _ems_registry[name.lower()] = cls
        return cls

    return decorator


# â”€â”€ Auto-discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _discover(package_name: str, registries: List[Dict[str, Any]]) -> None:
    """Auto-discover and register engine classes in ONE package.

    AUDIT (2026-10-22): this used to be called once per REGISTRY (4x) for each
    of 6 packages, so every module was imported and re-scanned 24 times. Because
    ``core.registry`` is imported by ``core.base``, importing ANY engine paid the
    full cost - measured at **47 s** for a single ``import blue_engine.core.pipeline``.
    The caller now passes all four registries at once, so each module is
    imported and inspected exactly once (24 scans -> 6).
    """
    try:
        package = importlib.import_module(package_name)
    except ImportError:
        return

    for mod_info in pkgutil.iter_modules(package.__path__):
        if mod_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{package_name}.{mod_info.name}")
        except Exception:  # noqa: BLE001 - a broken optional module must not
            # take the whole registry down at import time.
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if inspect.isabstract(obj):
                continue
            # NOTE: the old code had a no-op loop here
            # (``for base in registry.values(): if obj is base: continue``)
            # which did nothing; removed.
            for registry in registries:
                for base in list(registry.values()):
                    if issubclass(obj, base):
                        engine_name = (obj.__name__.replace("Engine", "")
                                       .replace("Slicer", "").lower())
                        registry.setdefault(engine_name, obj)
                        break


def discover_all(package_prefix: str = "blue_engine") -> None:
    """Auto-discover engine implementations across all blue_engine sub-packages.

    Args:
        package_prefix: The top-level package to scan (e.g., "blue_engine").

    AUDIT (2026-10-22): the four registries are passed together so each package
    is imported/scanned once instead of once per registry (a 4x saving).
    Callers that only need a specific registry should prefer the explicit
    ``register_*`` decorators.
    """
    # Engine sub-packages live FLAT under blue_engine/ -- there is deliberately
    # no intermediate `engines` package, so scan the flat sub-packages directly.
    registries = [_alpha_registry, _risk_registry, _oms_registry, _ems_registry]
    for sub in ("alpha", "economics", "market", "portfolio", "pov", "infrastructure"):
        _discover(f"{package_prefix}.{sub}", registries)


#: Discovery is DEFERRED (audit F-26). Running it at import time cost 47 s for a
#: single ``import`` because ``core.base`` imports this module. It now runs on
#: the first registry LOOKUP instead.
_DISCOVERED = False


def ensure_discovered() -> None:
    """Run auto-discovery exactly once, on first use."""
    global _DISCOVERED
    if not _DISCOVERED:
        _DISCOVERED = True
        discover_all("blue_engine")


def get_alpha_engine(name: str) -> Type[BaseAlphaEngine]:
    """Retrieve a registered Alpha engine by name."""
    ensure_discovered()
    name = name.lower()
    if name not in _alpha_registry:
        available = ", ".join(sorted(_alpha_registry.keys())) or "<none>"
        raise KeyError(f"Unknown Alpha engine '{name}'. Available: {available}")
    return _alpha_registry[name]


def get_risk_engine(name: str) -> Type[BaseRiskEngine]:
    """Retrieve a registered Risk engine by name."""
    ensure_discovered()
    name = name.lower()
    if name not in _risk_registry:
        available = ", ".join(sorted(_risk_registry.keys())) or "<none>"
        raise KeyError(f"Unknown Risk engine '{name}'. Available: {available}")
    return _risk_registry[name]


def get_oms_engine(name: str) -> Type[BaseOMSEngine]:
    """Retrieve a registered OMS engine by name."""
    name = name.lower()
    if name not in _oms_registry:
        available = ", ".join(sorted(_oms_registry.keys())) or "<none>"
        raise KeyError(f"Unknown OMS engine '{name}'. Available: {available}")
    return _oms_registry[name]


def get_ems_engine(name: str) -> Type[BaseEMSEngine]:
    """Retrieve a registered EMS engine by name."""
    name = name.lower()
    if name not in _ems_registry:
        available = ", ".join(sorted(_ems_registry.keys())) or "<none>"
        raise KeyError(f"Unknown EMS engine '{name}'. Available: {available}")
    return _ems_registry[name]


def list_engines(engine_type: Optional[EngineType] = None) -> Dict[str, list]:
    """List all registered engines, optionally filtered by type."""
    result = {
        "alpha": list(_alpha_registry.keys()),
        "risk": list(_risk_registry.keys()),
        "oms": list(_oms_registry.keys()),
        "ems": list(_ems_registry.keys()),
    }
    if engine_type:
        key = engine_type.value
        return {key: result.get(key, [])}
    return result


def is_registered(name: str, engine_type: EngineType) -> bool:
    """Check if an engine name is registered."""
    registries = {
        EngineType.ALPHA: _alpha_registry,
        EngineType.RISK: _risk_registry,
        EngineType.OMS: _oms_registry,
        EngineType.EMS: _ems_registry,
    }
    return name.lower() in registries.get(engine_type, {})


def unregister_engine(name: str, engine_type: EngineType) -> None:
    """Remove an engine from the registry (for plug-out support)."""
    registries = {
        EngineType.ALPHA: _alpha_registry,
        EngineType.RISK: _risk_registry,
        EngineType.OMS: _oms_registry,
        EngineType.EMS: _ems_registry,
    }
    registries.get(engine_type, {}).pop(name.lower(), None)


def clear_registry(engine_type: Optional[EngineType] = None) -> None:
    """Clear all engines from the registry (for testing/reset)."""
    if engine_type is None:
        _alpha_registry.clear()
        _risk_registry.clear()
        _oms_registry.clear()
        _ems_registry.clear()
    else:
        registries = {
            EngineType.ALPHA: _alpha_registry,
            EngineType.RISK: _risk_registry,
            EngineType.OMS: _oms_registry,
            EngineType.EMS: _ems_registry,
        }
        registries[engine_type].clear()


__all__ = [
    "EngineType",
    "BaseAlphaEngine",
    "BaseRiskEngine",
    "BaseOMSEngine",
    "BaseEMSEngine",
    "register_alpha_engine",
    "register_risk_engine",
    "register_oms_engine",
    "register_ems_engine",
    "get_alpha_engine",
    "get_risk_engine",
    "get_oms_engine",
    "get_ems_engine",
    "list_engines",
    "is_registered",
    "unregister_engine",
    "clear_registry",
    "discover_all",
    "ensure_discovered",
]