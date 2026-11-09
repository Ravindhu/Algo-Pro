"""Records every manual action (kill-switch reset, mode change, limit edit).

Flow stage: Override log
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/kill_switch_store.py
"""
from __future__ import annotations

from typing import Any


def record_override(audit: Any, actor: str, action: str, reason: str) -> str:
    """An override needs a named actor and a reason. Otherwise it is refused."""
    if not actor or not reason:
        raise ValueError("override needs an actor and a reason")
    return audit.append("override", {"actor": actor, "action": action, "reason": reason})
