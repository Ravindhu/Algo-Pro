"""Kill switch. Active blocks new entries. The file on disk is the only state.

Flow stage: Kill switch
Status: PHASE 1 IMPLEMENTED (file-backed since 2026-11-15)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/kill_switch_store.py

Every read goes back to the file. So a trip written by another process (the dashboard
STOP button) stops a running bot at its next entry check. A file that cannot be read is
treated as ON (fail closed).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Tuple


class KillSwitch:
    def __init__(self, path) -> None:
        self.path = Path(path)

    def _read(self) -> Tuple[bool, str]:
        if not self.path.exists():
            return False, ""
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            return bool(state["active"]), str(state.get("reason", ""))
        except (OSError, ValueError, KeyError, TypeError):
            return True, "kill switch file unreadable: treated as ON"

    @property
    def active(self) -> bool:
        return self._read()[0]

    @property
    def reason(self) -> str:
        return self._read()[1]

    def _save(self, active: bool, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"active": active, "reason": reason}), encoding="utf-8")
        os.replace(tmp, self.path)

    def trip(self, reason: str, source: str = "automatic") -> None:
        self._save(True, f"{source}: {reason}")

    def reset(self, actor: str, reason: str, audit: Optional[object] = None) -> None:
        """A person must reset it. The reset is written to the override log."""
        from quant.audit.override_log import record_override

        if audit is not None:
            record_override(audit, actor, "kill_switch_reset", reason)
        elif not actor or not reason:
            raise ValueError("reset needs an actor and a reason")
        self._save(False, "")
