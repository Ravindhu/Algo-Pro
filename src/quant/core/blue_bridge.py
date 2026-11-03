"""The only import bridge from src/quant to the existing Blue_engine package.

Flow stage: none (shared infrastructure, used by wrappers in every stage)
Status: PHASE 0 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/ (whole package)

WHY THIS FILE EXISTS
--------------------
Blue_engine code imports the package name ``blue_engine``. The folder on disk is
``Blue_engine/``. The installed package maps one to the other (Blue_engine/pyproject.toml).
The test suite does the same mapping by hand (Blue_engine/tests/conftest.py).
Production code had no such mapping, so this module adds it once, for everyone.

RULES
-----
- Only this file may import ``blue_engine``. Other src/quant modules call ``load()``.
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

#: src/quant/core/blue_bridge.py -> repo root is three levels up from ``core``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_DIR = _REPO_ROOT / "Blue_engine"


class BlueEngineUnavailable(ImportError):
    """Blue_engine (or its OpenAlgo SDK dependency) cannot be imported here."""


def _ensure_package() -> None:
    """Make ``blue_engine`` importable: installed package first, else the source folder."""
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
        raise BlueEngineUnavailable(f"Blue_engine source not found at {_SOURCE_DIR}")
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
            f"Blue_engine failed to load (is openalgo installed?): {exc}") from exc


def load(name: str) -> ModuleType:
    """Return the module ``blue_engine.<name>``, for example ``load("backtest.costs")``."""
    _ensure_package()
    try:
        return importlib.import_module(f"blue_engine.{name}")
    except ModuleNotFoundError as exc:
        raise BlueEngineUnavailable(f"blue_engine.{name} unavailable: {exc}") from exc
