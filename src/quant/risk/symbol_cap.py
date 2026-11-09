"""Max symbols per portfolio: a new symbol is blocked at the cap. Owner request 2026-11-07.

A symbol that is already held can still be added to. Null cap = not set = fail closed for new symbols.

Flow stage: Risk gate
Status: PHASE 3 IMPLEMENTED
"""
from __future__ import annotations

from typing import List, Set

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent


def symbol_cap_checks(intent: TradeIntent, open_symbols: Set[str], limits: RiskLimits) -> List[str]:
    """Reasons to block an entry because of the symbol cap. Empty list = passes."""
    cap = limits.max_symbols_per_portfolio
    if cap is None:
        return ["limit not set: max_symbols_per_portfolio"]
    if intent.symbol in open_symbols:
        return []
    if len(open_symbols) >= cap:
        return [f"max symbols per portfolio reached: {len(open_symbols)}/{cap}"]
    return []
