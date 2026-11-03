"""Survivorship-free universe: symbol membership by date, delisted names included.

Flow stage: Universe and actions
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/symbol_meta.py
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple


class Universe:
    def __init__(self) -> None:
        self._spans: Dict[str, Tuple[object, Optional[object]]] = {}

    def add(self, symbol: str, listed: object, delisted: Optional[object] = None) -> None:
        self._spans[symbol] = (listed, delisted)

    def is_member(self, symbol: str, ts) -> bool:
        if symbol not in self._spans:
            return False
        listed, delisted = self._spans[symbol]
        return listed <= ts and (delisted is None or ts < delisted)

    def members(self, ts) -> list:
        return sorted(s for s in self._spans if self.is_member(s, ts))
