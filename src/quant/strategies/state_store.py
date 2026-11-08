"""Paper state store: saves and loads open positions, strategy state and Handoff marks, so a restart resumes.

Flow stage: State persistence (paper)
Status: PHASE 2 IMPLEMENTED (paper only, 2026-11-08; not live).

The file is one JSON document, written to a temp file and then renamed (atomic on POSIX), so a crash
mid-write leaves the previous good state in place. The signal ledger is stored separately (scanner.py).
A version mismatch raises instead of guessing, so an old file is never loaded silently.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

STATE_VERSION = 1


def strategy_key(strategy: Any) -> str:
    return f"{strategy.name}|{strategy.symbol}"


def save_state(path: Path, *, positions: Any, strategies: Iterable[Any], handoff: Any = None) -> None:
    payload = {
        "version": STATE_VERSION,
        "positions": positions.to_state(),
        "strategies": {strategy_key(s): s.state() for s in strategies},
        "handoff": handoff.to_list() if handoff is not None else [],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_state(path: Path) -> Optional[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != STATE_VERSION:
        raise ValueError(f"state file {path} has version {data.get('version')}, expected {STATE_VERSION}")
    return data


def restore_all(data: Dict[str, Any], *, positions: Any, strategies: Iterable[Any], handoff: Any = None) -> None:
    """Apply loaded state: strategy state and handoff marks first, then open positions (which need the
    live strategy objects)."""
    by_key = {strategy_key(s): s for s in strategies}
    for key, st in data.get("strategies", {}).items():
        if key not in by_key:
            raise KeyError(f"saved state for unknown strategy {key}")
        by_key[key].restore(st)
    if handoff is not None:
        handoff.restore_list(data.get("handoff", []))
    positions.restore_state(data.get("positions", {}), by_key)
