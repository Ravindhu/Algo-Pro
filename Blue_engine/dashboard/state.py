"""
state.py - the small, explicit handoff object between a pipeline run and the
dashboard.

WHY THIS FILE EXISTS
====================
The dashboard and the trading runner are separate processes. Before this module
the only channel between them was "each page re-reads whatever it can find",
which meant a page could not show the result of the *last actual run* - it could
only show config.

The shift ledger (``blue_engine.core.agent_team``) is the natural handoff, so it
gets one:

  * the runner calls :func:`publish_shift` once per run;
  * it lands in ``<repo>/data/last_shift.json`` (atomic write, JSON only);
  * the dashboard calls :func:`read_shift` and gets the last real shift, or
    ``{}`` when no run has happened.

``LAST_SHIFT`` also mirrors the value in-process, so a dashboard that does run a
pipeline itself sees the result without touching the filesystem.

HONESTY RULES
-------------
* No run => ``{}``. The dashboard says "no shift" instead of inventing one.
* A corrupt, oversized or wrong-shaped file is treated as "no shift" and the
  reason is returned by :func:`read_shift_diagnostic`, never raised.
* Nothing here writes anything except the single shift file, and only JSON.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "SHIFT_FILENAME",
    "LAST_SHIFT",
    "shift_path",
    "publish_shift",
    "read_shift",
    "read_shift_diagnostic",
    "clear_shift",
]

#: Filename under ``<repo>/data``. One schema, one writer, one reader.
SHIFT_FILENAME = "last_shift.json"

#: Hard cap: a shift ledger is a few KB. Anything larger is not a ledger.
MAX_SHIFT_BYTES = 1 * 1024 * 1024

#: In-process mirror of the last published shift ({} = nothing published).
LAST_SHIFT: Dict[str, Any] = {}


def shift_path() -> Path:
    """``<repo>/data/last_shift.json``, derived from this file's location."""
    return Path(__file__).resolve().parents[2] / "data" / SHIFT_FILENAME


def _valid(shift: Any) -> bool:
    """A shift must be the ledger shape, not just any dict."""
    return (isinstance(shift, Mapping)
            and isinstance(shift.get("stations"), list)
            and isinstance(shift.get("product"), str)
            and bool(shift.get("product")))


def publish_shift(shift: Mapping[str, Any], *,
                  path: Optional[Path] = None) -> Dict[str, Any]:
    """Publish one shift for the dashboard. Never raises.

    Returns ``{"ok", "path", "reason"}``. A shift that does not match the ledger
    shape is refused rather than written, so the dashboard can trust the file.
    """
    global LAST_SHIFT
    if not _valid(shift):
        return {"ok": False, "path": None,
                "reason": "refused: not a shift ledger"}
    payload = dict(shift)
    target = Path(path) if path else shift_path()
    LAST_SHIFT = payload
    try:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2,
                             allow_nan=False, default=str)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                        dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                try:
                    os.fsync(stream.fileno())
                except OSError:  # pragma: no cover - platform quirk
                    pass
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    except OSError as exc:
        logger.warning("publish_shift: write failed: %r", exc)
        return {"ok": False, "path": str(target), "reason": f"{exc!r}"}
    return {"ok": True, "path": str(target), "reason": "published"}


def read_shift_diagnostic(*, path: Optional[Path] = None
                          ) -> Tuple[Dict[str, Any], str]:
    """Return ``(shift, reason)``. ``shift`` is ``{}`` when there is none."""
    if _valid(LAST_SHIFT):
        return dict(LAST_SHIFT), "in-process"
    target = Path(path) if path else shift_path()
    if not target.is_file():
        return {}, "no shift file"
    try:
        if target.stat().st_size > MAX_SHIFT_BYTES:
            return {}, "shift file is too large"
        with target.open("r", encoding="utf-8") as stream:
            loaded = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"unreadable: {type(exc).__name__}"
    if not _valid(loaded):
        return {}, "shift file has the wrong shape"
    return dict(loaded), "file"


def read_shift(*, path: Optional[Path] = None) -> Dict[str, Any]:
    """The last real shift, or ``{}``. Never raises."""
    return read_shift_diagnostic(path=path)[0]


def clear_shift() -> None:
    """Drop the in-process mirror (tests, or an explicit operator reset)."""
    global LAST_SHIFT
    LAST_SHIFT = {}
