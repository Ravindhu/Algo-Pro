"""BUY strategies (OCL, OCL sell-fail TOP/BOTTOM, engulfing OCL), registered by name.

Ported from Algo-Lab on 2026-11-08 for PAPER trading only. See
docs/IMPLEMENTATION_PLAN_2026-11-08_PAPER_BUY_SCANNER.md.
Strategies only produce signals. They never size orders, never change risk limits and never call a broker.
"""
from quant.strategies.registry import BUY_STRATEGIES, build

__all__ = ["BUY_STRATEGIES", "build"]
