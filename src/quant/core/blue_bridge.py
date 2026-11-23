"""The only import bridge from src/quant to the engine package (src/quant/engines/).

Flow stage: none (shared infrastructure, used by wrappers in every stage)
Status: PHASE 0 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/ (whole package)

WHY THIS FILE EXISTS
--------------------
The engine source lives at ``src/quant/blue_engine/`` and imports itself as
``blue_engine``. The folder name and the package name now differ only by case,
so on Windows a plain import would find it, but on Linux (case-sensitive) it
would not. This module installs the package mapping once, for everyone, so the
same code runs on both.

RULES
-----
- Only this file may import ``blue_engine``. Other src/quant modules call ``load()``.
- The vendored OpenAlgo SDK is a fallback on sys.path (decision 0014).
- If the OpenAlgo SDK is missing, ``load()`` raises ``BlueEngineUnavailable`` with the
  reason. It never returns a half-loaded module.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

__all__ = ["BlueEngineUnavailable", "load"]

#: Engine package now lives inside quant itself: src/quant/engines/.
_SOURCE_DIR = Path(__file__).resolve().parents[1] / "engines"


class BlueEngineUnavailable(ImportError):
    """The engine package (or its OpenAlgo SDK dependency) cannot be imported here."""


#: Vendored OpenAlgo SDK (Temp/openalgo, MIT; decision 0014). The SDK package is
#: ``Temp/openalgo``, so the sys.path entry is its parent, ``Temp``.
#: src/quant/core/blue_bridge.py -> repo root is three levels up from ``core``.
_VENDOR_DIR = Path(__file__).resolve().parents[3] / "Temp"


def _ensure_sdk_on_path() -> None:
    """Add the vendored SDK as a FALLBACK. Appended, so a pip-installed SDK still wins."""
    if (_VENDOR_DIR / "openalgo").is_dir() and str(_VENDOR_DIR) not in sys.path:
        sys.path.append(str(_VENDOR_DIR))


def _ensure_package() -> None:
    """Import ``quant.engines`` once. Its own __init__ fixes the SDK import path."""
    _ensure_sdk_on_path()
    if "quant.engines" in sys.modules:
        return
    if not (_SOURCE_DIR / "__init__.py").is_file():
        raise BlueEngineUnavailable(f"engine package not found at {_SOURCE_DIR}")
    try:
        importlib.import_module("quant.engines")
    except ModuleNotFoundError as exc:
        if exc.name not in ("quant.engines",):
            # A real dependency is missing (for example openalgo). Report it, do not hide it.
            raise BlueEngineUnavailable(f"engine dependency missing: {exc}") from exc
        raise BlueEngineUnavailable(f"engine package unavailable: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - leave no half-loaded package behind
        for key in [k for k in sys.modules if k == "quant.engines" or k.startswith("quant.engines.")]:
            sys.modules.pop(key, None)
        raise BlueEngineUnavailable(
            f"engine package failed to load (is openalgo installed?): {exc}") from exc


def load(name: str) -> ModuleType:
    """Return the module ``quant.engines.<name>``, for example ``load("backtest.costs")``."""
    _ensure_package()
    try:
        return importlib.import_module(f"quant.engines.{name}")
    except ModuleNotFoundError as exc:
        raise BlueEngineUnavailable(f"quant.engines.{name} unavailable: {exc}") from exc
