"""
base.py - re-export shim.

``ocl_keylevel.py`` uses ``from .base import BaseSignal``. The real
implementation lives in ``blue_engine.alpha.signals.base``. This file
makes the relative import resolve so the OCL file does not need to be
edited.
"""
from blue_engine.alpha.signals.base import BaseSignal  # noqa: F401

__all__ = ["BaseSignal"]
