"""Read config/risk_limits.yaml once per process, fail closed on unreadable.

Audit item 5 (2026-11-17): the owner's hard caps only reached the blue runner.
The v5 risk gate now applies them on EVERY intent, so it needs a loader that is
cheap on the hot path and never lets a broken file look like "no limits".

Fail-closed rules (owner decision 0037, same as blue owner_limits):
- file missing/unreadable/unparseable -> ``BLOCKED`` sentinel: the gate refuses
  entries rather than trading without the owner's caps.
- a null cap -> absent from the dict: not set, no threshold invented.

Flow stage: Risk gate
Status: PHASE 1 IMPLEMENTED
"""
from __future__ import annotations

import copy
import pathlib
from typing import Any, Dict, Optional

import yaml

#: Sentinel returned when the owner file cannot be read. The gate treats this
#: as "entries refused": no cap can be verified, so nothing may be sent.
BLOCKED: Dict[str, Any] = {"blocked": True}

#: Repo root is four levels up from src/quant/core/.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "config" / "risk_limits.yaml"

_cached: Dict[str, Any] = {}
_loaded_from: Optional[str] = None
_loaded_stamp: Optional[tuple] = None


def _stamp(target: pathlib.Path) -> Optional[tuple]:
    """(mtime_ns, size) of the limits file, or None when it cannot be statted."""
    try:
        st = target.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None

#: Keys the gate enforces directly. They mirror RiskLimits' monetary caps.
GATE_KEYS = ("max_order_quantity", "max_order_notional",
             "max_daily_loss", "max_open_exposure", "blocked")


def load(path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """The owner's limits, re-read when the file changes. Never raises.

    A caller can pass ``path`` (tests do). The read is cached but keyed on the
    file's mtime and size, so a dashboard write to risk_limits.yaml reaches a
    running bot on its next intent (audit fix 5, 2026-11-26). A failed read is
    NOT cached: a file fixed at runtime is picked up on the next intent.
    """
    global _cached, _loaded_from, _loaded_stamp
    target = pathlib.Path(path) if path is not None else DEFAULT_PATH
    stamp = _stamp(target)
    if _loaded_from == str(target) and _cached is not BLOCKED and stamp is not None \
            and stamp == _loaded_stamp:
        return _cached
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return BLOCKED
    if not isinstance(data, dict):
        return BLOCKED
    _cached = {k: data.get(k) for k in GATE_KEYS if data.get(k) is not None}
    _cached["blocked"] = bool(data.get("blocked", False))
    _loaded_from = str(target)
    _loaded_stamp = stamp
    return copy.deepcopy(_cached)


def reset() -> None:
    """Drop the cache (tests, and a config change that must be re-read)."""
    global _cached, _loaded_from, _loaded_stamp
    _cached = {}
    _loaded_from = None
    _loaded_stamp = None
