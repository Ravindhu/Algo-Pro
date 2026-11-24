"""alpha - strategy package (signal strategies that decide WHEN to trade).

The ``AlphaEngine`` scoring stage was retired on 2026-11-24 and parked in
``Temp/Stag/alpha_engine``. The strategy signals below are the live trading
strategy and stay here. See docs/IMPLEMENTATION_PLAN_2026-11-24_RETIRE_ALPHA.md.
"""
from . import signals

__all__ = ["signals"]