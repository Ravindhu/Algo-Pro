"""HRP entry point. The implementation is local since 2026-11-11 (decoupling stage 3, item 8).

Flow stage: HRP optimizer
Status: PHASE 3 IMPLEMENTED (local; no Blue import)

Kept as a separate module so call sites and tests that import
``quant.portfolio.optimizer.hrp_weights`` keep working.
"""
from __future__ import annotations

from quant.portfolio.hrp import hrp_weights

__all__ = ["hrp_weights"]
