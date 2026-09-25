"""
run_history.py - append-only JSONL store of economic-engine runs.

This is the bridge between the (pure) engine and the dashboard. The engine
itself performs no I/O; the *runner* (or the engine when ``persist=True``) calls
:func:`record` once per run, and the Dash page reads the history back to chart
the regime path over time.

Layout
------
    data/economic_runs/YYYY-MM-DD.jsonl      one JSON object per line

Each line is the full economic stage payload plus a timestamp::

    {"ts": 1730000000.0, "regime": "RISK_ON", "risk_on_score": 0.61,
     "gross_exposure_multiplier": 1.45, "signals": {...}, ...}

Why JSONL (not a DB)
--------------------
* append-only, crash-safe (a partial last line is simply ignored on read),
* human-readable for forensics,
* zero dependencies, zero schema migrations,
* trivially tail-able while the engine runs.

Everything here is best-effort: a disk error is logged and swallowed, because a
bookkeeping miss must never abort a trading run (same rule as the ledger).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# <repo>/data/economic_runs/   (sibling-safe, survives an openalgo re-clone)
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = _REPO_ROOT / "data" / "economic_runs"


def _dir(path: Optional[Path] = None) -> Path:
    return Path(path) if path else DEFAULT_DIR


def record(payload: Dict[str, Any], *, ts: Optional[float] = None,
           directory: Optional[Path] = None,
           run_id: Optional[str] = None) -> Optional[Path]:
    """Append one run to today's JSONL file. Never raises.

    Returns the file path written to, or ``None`` on failure.
    """
    try:
        stamp = float(ts if ts is not None else time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(stamp))
        out_dir = _dir(directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{day}.jsonl"

        line = {"ts": stamp}
        if run_id:
            line["run_id"] = run_id
        # Shallow-merge the payload; keep it JSON-serialisable (fall back to
        # str() for anything exotic so a single bad value cannot drop the line).
        for k, v in (payload or {}).items():
            line[k] = v
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
        return path
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never abort a run
        logger.warning("economic run_history.record failed: %s", exc)
        return None


def read_recent(*, days: int = 14, limit: int = 500,
                directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read the most recent runs across the last ``days`` daily files.

    Oldest-first. Malformed lines are skipped (so a torn tail write is safe).
    """
    out_dir = _dir(directory)
    if not out_dir.is_dir():
        return []
    files = sorted(out_dir.glob("*.jsonl"))[-max(1, days):]
    rows: List[Dict[str, Any]] = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rows.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.debug("economic run_history read failed for %s: %s", path, exc)
    return rows[-limit:] if limit else rows


def latest(directory: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Return the single most recent run, or None."""
    rows = read_recent(days=7, limit=1, directory=directory)
    return rows[-1] if rows else None


def available(directory: Optional[Path] = None) -> bool:
    """True when at least one run file exists."""
    return _dir(directory).is_dir() and any(_dir(directory).glob("*.jsonl"))


__all__ = ["record", "read_recent", "latest", "available", "DEFAULT_DIR"]