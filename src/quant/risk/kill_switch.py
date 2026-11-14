"""Kill switch. Active blocks new entries. State is saved, so a restart keeps it on.

Flow stage: Kill switch
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/kill_switch_store.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


class KillSwitch:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.active = False
        self.reason = ""
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding="utf-8"))
            self.active, self.reason = bool(state["active"]), state["reason"]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"active": self.active, "reason": self.reason}), encoding="utf-8")

    def trip(self, reason: str, source: str = "automatic") -> None:
        self.active, self.reason = True, f"{source}: {reason}"
        self._save()

    def reset(self, actor: str, reason: str, audit: Optional[object] = None) -> None:
        """A person must reset it. The reset is written to the override log."""
        from quant.audit.override_log import record_override

        if audit is not None:
            record_override(audit, actor, "kill_switch_reset", reason)
        elif not actor or not reason:
            raise ValueError("reset needs an actor and a reason")
        self.active, self.reason = False, ""
        self._save()
