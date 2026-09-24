"""
slicers/ — auto-registers every BaseSlicer subclass found in this package.

The POV slicer is the default execution algorithm. To add a new slicer
(e.g., TWAP, VWAP, Iceberg):
    1. Create a new file in this directory
    2. Define a class that inherits from BaseSlicer
    3. Reference it from a JSON config via the "slicer" key
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Dict, Type

from .base import BaseSlicer

_REGISTRY: Dict[str, Type[BaseSlicer]] = {}


def _register_all() -> None:
    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name.startswith("_") or mod_info.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{mod_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is BaseSlicer:
                continue
            if not issubclass(obj, BaseSlicer):
                continue
            if inspect.isabstract(obj):
                continue
            name = obj.__name__
            if name.endswith("Slicer"):
                name = name[:-6]
            _REGISTRY[name.lower()] = obj


_register_all()


def get(name: str) -> Type[BaseSlicer]:
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "<none>"
        raise KeyError(
            f"Unknown slicer '{name}'. Available slicers: {available}"
        )
    return _REGISTRY[name]


def available() -> list[str]:
    return sorted(_REGISTRY.keys())
