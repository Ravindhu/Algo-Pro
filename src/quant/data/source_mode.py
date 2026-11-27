"""Which symbol sources feed the bot: M = manual symbols, A = Screener.in automation (2026-11-27).

Rule (owner request):
  * M on, A off  -> the bot uses the manual symbols only.
  * A on, M off  -> the bot uses the Screener.in selection only.
  * Both on, or both off -> both paths work.

File: ``data/screened/source_mode.json`` = ``{"manual": bool, "automation": bool}``.
Never raises on read. Writes are atomic (temp file + os.replace).

Flow stage: Liquidity screener (owner switch; decides which candidate sources are refreshed)
Status: PHASE 3 IMPLEMENTED (2026-11-27; read/write only the mode file, no order path)
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

KEYS = ("manual", "automation")


#: Fail closed (owner decision 2026-11-28): a missing or broken file means MANUAL symbols only.
#: The Screener.in path, which needs a live scrape, is never used unless its switch is turned on.
FAILSAFE = {"manual": True, "automation": False}


def read(path) -> Dict[str, bool]:
    """Current switches. A missing or broken file gives the FAILSAFE state: manual symbols only."""
    failsafe = dict(FAILSAFE)
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return failsafe
    if not isinstance(payload, dict):
        return failsafe
    return {key: payload.get(key) is True for key in KEYS}


def set_flag(path, key: str, on: bool) -> Dict[str, bool]:
    """Turn one switch on or off and save. Returns the new state. Raises ValueError for an unknown key."""
    if key not in KEYS:
        raise ValueError(f"unknown source switch: {key}")
    state = read(path)
    state[key] = bool(on)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return state


def effective(state: Dict[str, Any]) -> Tuple[bool, bool]:
    """``(use_manual, use_screener)`` for a state. Exactly one switch on picks that path only."""
    manual = state.get("manual") is True
    automation = state.get("automation") is True
    if manual and not automation:
        return True, False
    if automation and not manual:
        return False, True
    return True, True  # both on, or both off: both paths work
