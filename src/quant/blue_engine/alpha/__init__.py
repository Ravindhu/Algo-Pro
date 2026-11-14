"""alpha - Alpha engine package.

Also hosts the ``signals`` sub-package (the signal strategies that decide
WHEN to trade) -- signal generation is the alpha stage's responsibility.
"""
from .alpha_engine import AlphaEngine, DEFAULT_FUNDAMENTAL_RULES
from . import signals

__all__ = ["AlphaEngine", "DEFAULT_FUNDAMENTAL_RULES", "signals"]