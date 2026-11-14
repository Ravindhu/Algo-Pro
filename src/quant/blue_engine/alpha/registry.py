"""
alpha/registry.py - read-only views over the live trading system.

WHAT THIS MODULE IS FOR
-----------------------
The optimizer dashboard's Auto-Live mode needs three things from the
live system, and ONLY three things:

  1. The list of currently-registered strategies (so the dropdown
     reflects what the alpha engine actually has available).
  2. The currently-active strategy/symbol/interval/params that the
     live runner is configured with (so Auto-Live can mirror it).
  3. A way to resolve a strategy name into a callable (so the
     optimizer engine can run a study on it).

This module is the **single read-only adapter** between the live
trading system and the optimizer. It does NOT:
  * start or stop any background process
  * call the broker
  * place orders
  * write to the live config
  * import pandas

Everything here is a pure function of the file system and the
in-process registry. If the file is missing, the function returns
``None`` and the caller surfaces a clear message.

DESIGN
------
The plan in ``Temp/OPTIMIZER_HEADER_PLAN.md`` (section "Auto-Live
wiring") said the live runner writes
``blue_engine/input/strategy.active.json`` on every config change.
The actual project may not yet have a live runner that writes this
file, so the reader treats the file as **optional**: missing means
"no live system is active", and the caller falls back to offline
mode.

This module NEVER raises on a missing file. It returns ``None``.
That is the contract; the dashboard and the CLI rely on it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #

#: Location of the live runner's active-state file. The file is OPTIONAL;
#: when missing, ``live_active_state()`` returns ``None`` and the caller
#: should fall back to the offline (manual) workflow.
DEFAULT_ACTIVE_PATH = Path(__file__).resolve().parents[1] / "input" / "strategy.active.json"


__all__ = [
    "live_active_state",
    "list_strategies",
    "strategy_module",
    "DEFAULT_ACTIVE_PATH",
]


# --------------------------------------------------------------------------- #
# Strategy registry (read-only view over alpha.signals.available())
# --------------------------------------------------------------------------- #


def list_strategies() -> List[Dict[str, Any]]:
    """List every strategy the alpha engine currently has registered.

    Returns a list of dicts (sorted by ``name``) with the fields
    the dashboard's strategy dropdown needs::

        {
            "name": "ocl_keylevel",
            "label": "OCL Keylevel",
            "module": "blue_engine.alpha.Statergy.ocl_keylevel",
            "kind": "live",          # or "signal"
            "description": "..."     # first line of the docstring
        }

    The list is the UNION of the two folders the auto-registrar
    walks: ``alpha/signals/`` (the flat folder) and
    ``alpha/Statergy/`` (the OCL keylevel and friends). The
    ``kind`` field tells the dashboard where the strategy lives
    so it can show a "live" or "signal" badge in the dropdown.

    Read-only. Never raises. On import failure, returns an empty
    list and logs a warning.
    """
    out: List[Dict[str, Any]] = []
    try:
        # Importing the package triggers the auto-registrar in
        # ``alpha/signals/__init__.py:_register_strategies``.
        from blue_engine.alpha.signals import available, get
    except Exception as exc:  # noqa: BLE001
        logger.warning("alpha registry: cannot import available(): %s", exc)
        return out

    # ``available()`` returns a list of *names* (strings). The
    # corresponding class is fetched via ``get(name)``, which is the
    # canonical lookup the rest of the system uses.
    for name in available():
        cls = get(name)
        if cls is None:
            continue
        module = getattr(cls, "__module__", "") or ""
        # "live" = the strategy was discovered by the alpha/Statergy
        # walker (i.e. it knows about a live runner / client). "signal"
        # = the strategy was discovered in alpha/signals (pure signal
        # code with no live runner). This distinction is informational;
        # both are usable in the optimizer.
        kind = "live" if "alpha.Statergy" in module else "signal"
        label = getattr(cls, "label", name) or name
        doc = (getattr(cls, "__doc__", "") or "").strip()
        first_line = doc.splitlines()[0] if doc else ""
        out.append({
            "name": name,
            "label": label,
            "module": module,
            "kind": kind,
            "description": first_line,
        })
    out.sort(key=lambda d: d["name"])
    return out


def strategy_module(name: str) -> Optional[str]:
    """Return the dotted module path of a registered strategy.

    Convenience helper for the CLI: ``strategy_module("ocl_keylevel")``
    returns ``"blue_engine.alpha.Statergy.ocl_keylevel"``. Returns
    ``None`` when the name is not registered.
    """
    for entry in list_strategies():
        if entry["name"] == name:
            return entry["module"]
    return None


# --------------------------------------------------------------------------- #
# Live active state (read-only view over strategy.active.json)
# --------------------------------------------------------------------------- #


def live_active_state(
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Return the live trading system's currently-active strategy state.

    Reads ``blue_engine/input/strategy.active.json`` (or the
    ``path`` override). The file shape is::

        {
            "strategy": "ocl_keylevel",
            "module": "blue_engine.alpha.Statergy.ocl_keylevel",
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "interval": "5minute",
            "started_at": "2026-10-01T09:15:00+05:30",
            "params": { "htf_interval": "15minute", "min_body_pct": 0.3, ... }
        }

    Returns ``None`` when:
      * the file does not exist (no live runner, or hasn't booted
        yet — the normal case for a freshly-installed project), or
      * the file is empty / unreadable, or
      * the file is not valid JSON.

    Returns ``{"error": "..."}`` (NOT ``None``) when the file
    EXISTS but is malformed. That is a different failure mode: the
    file is present, so the live runner thinks it has a config; the
    reader can't parse it. The dashboard should surface this as a
    "live config unreadable" panel, NOT as "no live config".

    Read-only. Never writes to disk. Never imports the live
    runner, the broker, or OpenAlgo.
    """
    target = Path(path) if path is not None else DEFAULT_ACTIVE_PATH
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        # File exists but is unreadable (permissions, locked). This
        # is the "live config unreadable" failure mode.
        return {"error": f"cannot read {target}: {exc}"}
    if not raw.strip():
        # Empty file. Treat as "no live config" rather than
        # "broken live config" — an empty file usually means the
        # runner just hasn't written yet.
        return None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"error": f"invalid JSON in {target}: {exc}"}
    if not isinstance(state, dict):
        return {"error": f"{target} must be a JSON object, got {type(state).__name__}"}
    return state
