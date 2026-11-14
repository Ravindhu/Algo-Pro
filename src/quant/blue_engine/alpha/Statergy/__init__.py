"""
Statergy/__init__.py - shim so ocl_keylevel.py's relative imports work.

The OCL keylevel file uses ``from .base import BaseSignal`` and
``from ._pattern import ...``. Those modules live in
``blue_engine/alpha/signals/``, not here. This shim re-exports them so
the OCL file's relative imports resolve correctly when the file lives
in this folder.

Adding a new strategy to this folder is now zero-config:
  1. Drop a new ``<name>.py`` in this folder
  2. Define a ``BaseSignal`` subclass
  3. (Optional) set ``name = "<keylevel_name>"`` as a class attribute
     to control the registered name; otherwise the lowercase class
     name with the trailing ``Signal`` suffix stripped is used.

The ``blue_engine.alpha.signals._register_strategies`` walker
discovers the new file at the next import.
"""
from __future__ import annotations

# Re-export BaseSignal and the OCL pattern helpers so the file's
# relative imports ``from .base import BaseSignal`` and
# ``from ._pattern import ...`` resolve to the real implementations
# that live in ``blue_engine.alpha.signals``.
from blue_engine.alpha.signals.base import BaseSignal
from blue_engine.alpha.signals._pattern import (  # noqa: F401
    action_for_pattern,
    calculate_trigger_level,
    extract_ohlc,
    validate_pattern,
)

__all__ = [
    "BaseSignal",
    "action_for_pattern",
    "calculate_trigger_level",
    "extract_ohlc",
    "validate_pattern",
]
