"""Safe mode controller: normal, reduce-only, halt-all. Every change is logged with a reason.

Flow stage: Safe mode controller
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/mode.py
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class Mode(str, Enum):
    NORMAL = "normal"
    REDUCE_ONLY = "reduce_only"
    HALT_ALL = "halt_all"


class SafeMode:
    def __init__(self, audit: Optional[object] = None) -> None:
        self.mode = Mode.NORMAL
        self.audit = audit

    def set(self, mode: Mode, reason: str) -> None:
        if not reason:
            raise ValueError("safe mode change needs a reason")
        old, self.mode = self.mode, Mode(mode)
        if self.audit is not None:
            self.audit.append("safe_mode", {"from": old.value, "to": self.mode.value, "reason": reason})
