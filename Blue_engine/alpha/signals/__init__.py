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


def _register_from_module(module: Any) -> int:
    """Register every ``BaseSignal`` subclass found in ``module``.

    Returns the number of NEW registrations (i.e. excludes already-known
    names from a previous walker pass). Skips the abstract base and any
    class that is not a true subclass. Reused by both the
    ``alpha.signals`` walker and the ``alpha.Statergy`` walker so they
    behave identically.
    """
    if module is None:
        return 0
    try:
        members = inspect.getmembers(module, inspect.isclass)
    except Exception as exc:  # noqa: BLE001
        logger.warning("signals: introspect failed for %r: %s", module, exc)
        return 0
    before = len(_REGISTRY)
    for _, obj in members:
        if obj is BaseSignal:
            continue
        if not issubclass(obj, BaseSignal):
            continue
        if inspect.isabstract(obj):
            continue
        name = obj.__name__
        if name.endswith("Signal"):
            name = name[:-6]
        # Prefer an explicit class-level ``name`` (set by the strategy
        # author) over the auto-derived one. This lets a class named
        # ``OCLKeylevelSignal`` register as ``ocl_keylevel`` (matching
        # its filename) instead of the auto-derived ``keylevel`` (which
        # would collide with other "Keylevel" strategies).
        registry_name = getattr(obj, "name", None) or name
        _REGISTRY[registry_name.lower()] = obj
    return len(_REGISTRY) - before


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
        _register_from_module(module)


# ---- Second walker: Blue_engine.alpha.Statergy/<name>.py --------------- #
#
# The user wants every .py file in ``Blue_engine/alpha/Statergy/`` to
# appear in the optimizer strategy dropdown. The flat layout is the
# canonical path for runtime strategies (per the locked answer #2 in
# ``Temp/OPTIMIZER_HEADER_PLAN.md``). New strategies drop into that
# folder and are picked up here, with NO code change.
#
# This walker is dynamic: it scans the package directory at import time
# using ``pkgutil.iter_modules``, the same approach the ``alpha.signals``
# walker uses for its own package. A new file in either folder shows
# up in ``available()`` on the next process start.
def _register_strategies() -> None:
    """Walk ``blue_engine.alpha.Statergy`` and register every ``BaseSignal`` subclass.

    A drop-in replacement for the older hand-rolled tuple. The walker
    scans the package directory at import time; a new file added to
    ``alpha/Statergy/`` is picked up on the next process start. The
    existing ``alpha.signals`` walker is unchanged.
    """
    import importlib as _il
    try:
        pkg = _il.import_module("blue_engine.alpha.Statergy")
    except Exception as exc:  # noqa: BLE001
        logger.warning("strategies: cannot import blue_engine.alpha.Statergy: %s", exc)
        return
    pkg_path = getattr(pkg, "__path__", None)
    if not pkg_path:
        return
    for mod_info in pkgutil.iter_modules(pkg_path):
        if mod_info.name.startswith("_"):
            continue
        module_name = f"blue_engine.alpha.Statergy.{mod_info.name}"
        try:
            module = _il.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("strategies: skipping %s: %s", module_name, exc)
            continue
        new = _register_from_module(module)
        if new:
            logger.info("strategies: registered %d new signal(s) from %s",
                         new, module_name)


_register_all()
_register_strategies()


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
