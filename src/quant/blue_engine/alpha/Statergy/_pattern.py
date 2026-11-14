"""
_pattern.py - re-export shim.

``ocl_keylevel.py`` uses ``from ._pattern import ...``. The real
implementation lives in ``blue_engine.alpha.signals._pattern``. This
file makes the relative import resolve so the OCL file does not need
to be edited.

Zero-config for new strategies: drop a new file in this folder and the
``_register_strategies`` walker discovers it automatically.
"""
from blue_engine.alpha.signals._pattern import (  # noqa: F401
    action_for_pattern,
    calculate_trigger_level,
    extract_ohlc,
    validate_pattern,
)

__all__ = [
    "action_for_pattern",
    "calculate_trigger_level",
    "extract_ohlc",
    "validate_pattern",
]
