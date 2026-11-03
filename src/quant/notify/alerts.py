"""Alerts by level. CRITICAL calls a handler, normally the kill switch.

Flow stage: Alerts
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/supervisor.py
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

LEVELS = ("INFO", "WARNING", "ERROR", "CRITICAL")


class Alerts:
    def __init__(self, on_critical: Optional[Callable[[str], None]] = None) -> None:
        self.on_critical = on_critical
        self.items: List[Dict[str, str]] = []

    def emit(self, level: str, component: str, message: str) -> None:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        self.items.append({"level": level, "component": component, "message": message})
        if level == "CRITICAL" and self.on_critical is not None:
            self.on_critical(f"{component}: {message}")
