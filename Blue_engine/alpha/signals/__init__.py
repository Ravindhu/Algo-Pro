"""
signals/ - auto-registers every BaseSignal subclass found in this package.

This package lives under the Alpha engine (blue_engine/alpha/signals/):
a signal is an alpha decision -- it decides WHEN to trade. The POV engine
(blue_engine/pov/slicers/) decides HOW to slice the resulting order.

To add a new signal strategy:
    1. Create a new file in this directory (e.g., engulfing.py)
    2. Define a class that inherits from BaseSignal
    3. The class becomes available by name (minus 'Signal' suffix) in
       blue_engine/input/<your_config>.json under the "signal" key.

The auto-registration below scans the package directory and indexes every
concrete BaseSignal subclass. No manual registry edits are needed.

Failure tolerance: a module that fails to import (e.g. the openalgo
SDK is not installed in a thin test env) is skipped, not raised. The
helper modules ``_pattern.py`` and ``_template.py`` (whose names start
with ``_``) are not loaded here at all.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import Any, Dict, Type

from .base import BaseSignal

logger = logging.getLogger(__name__)

# The class is registered by its lowercase class name with the trailing
# 'Signal' suffix stripped. Example: ManualSignal -> "manual"
_REGISTRY: Dict[str, Type[BaseSignal]] = {}


def _register_all() -> None:
    """Walk every module in this package and register its BaseSignal classes.

    Import errors for individual signal modules are logged and skipped
    rather than raised, so a single broken signal does not prevent the
    rest of the package (or sibling packages like the OCL screener)
    from loading.
    """
    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name.startswith("_") or mod_info.name == "base":
            continue
        module_name = f"{__name__}.{mod_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("signals: skipping %s: %s", module_name, exc)
            continue
        try:
            members = inspect.getmembers(module, inspect.isclass)
        except Exception as exc:  # noqa: BLE001
            logger.warning("signals: introspect failed for %s: %s", module_name, exc)
            continue
        for _, obj in members:
            if obj is BaseSignal:
                continue
            if not issubclass(obj, BaseSignal):
                continue
            if inspect.isabstract(obj):
                continue
            # Register as lowercase class name without 'Signal' suffix
            name = obj.__name__
            if name.endswith("Signal"):
                name = name[:-6]
            key = name.lower()
            _REGISTRY[key] = obj


_register_all()


def get(name: str) -> Type[BaseSignal]:
    """Look up a signal class by its registered name.

    Args:
        name: Lowercase short name, e.g. "manual" for ManualSignal.

    Raises:
        KeyError: If the name is not registered.
    """
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "<none>"
        raise KeyError(
            f"Unknown signal '{name}'. Available signals: {available}"
        )
    return _REGISTRY[name]


def available() -> list[str]:
    """Return the sorted list of registered signal names."""
    return sorted(_REGISTRY.keys())
