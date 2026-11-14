"""The only import bridge from src/quant to the vendored engine package.

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

#: src/quant/core/blue_bridge.py -> src/quant is two levels up from ``core``.
_SOURCE_DIR = Path(__file__).resolve().parents[1] / "blue_engine"


class BlueEngineUnavailable(ImportError):
    """Blue_engine (or its OpenAlgo SDK dependency) cannot be imported here."""


#: Vendored OpenAlgo SDK (src/quant/vendor/openalgo, MIT; decision 0014).
_VENDOR_DIR = Path(__file__).resolve().parents[1] / "vendor"


def _ensure_sdk_on_path() -> None:
    """Add the vendored SDK as a FALLBACK. Appended, so a pip-installed SDK still wins."""
    if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
        sys.path.append(str(_VENDOR_DIR))


def _ensure_package() -> None:
    """Make ``blue_engine`` importable: installed package first, else the source folder."""
    _ensure_sdk_on_path()
    if "blue_engine" in sys.modules:
        return
    try:
        importlib.import_module("blue_engine")
        return
    except ModuleNotFoundError as exc:
        if exc.name != "blue_engine":
            # A real dependency is missing (for example openalgo). Report it, do not hide it.
            raise BlueEngineUnavailable(f"Blue_engine dependency missing: {exc}") from exc

    init_file = _SOURCE_DIR / "__init__.py"
    if not init_file.is_file():
        raise BlueEngineUnavailable(f"blue_engine source not found at {_SOURCE_DIR}")
    spec = importlib.util.spec_from_file_location(
        "blue_engine", init_file, submodule_search_locations=[str(_SOURCE_DIR)])
    if spec is None or spec.loader is None:
        raise BlueEngineUnavailable("cannot build an import spec for blue_engine")
    module = importlib.util.module_from_spec(spec)
    sys.modules["blue_engine"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - leave no half-loaded package behind
        sys.modules.pop("blue_engine", None)
        raise BlueEngineUnavailable(
            f"blue_engine failed to load (is openalgo installed?): {exc}") from exc


def load(name: str) -> ModuleType:
    """Return the module ``blue_engine.<name>``, for example ``load("backtest.costs")``."""
    _ensure_package()
    try:
        return importlib.import_module(f"blue_engine.{name}")
    except ModuleNotFoundError as exc:
        raise BlueEngineUnavailable(f"blue_engine.{name} unavailable: {exc}") from exc
