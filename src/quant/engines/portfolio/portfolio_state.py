"""
portfolio_state.py - append-only JSONL store of portfolio-engine runs.

Mirrors ``economics/run_history.py`` exactly (same layout, same crash-safety, same
"best-effort, never abort a run" rule). The engine itself performs no I/O; the
runner (or the engine when ``persist=True``) calls :func:`record` once per run,
and the Dash pages read the history back to chart target drift, gross path and
turnover over time.

Layout
------
    data/portfolio_runs/YYYY-MM-DD.jsonl      one JSON object per line

Each line is the portfolio stage payload plus a timestamp and, optionally, the
snapshot needed by ``attribution.drift``::

    {"ts": ..., "regime": "RISK_ON", "optimiser": "hrp",
     "target_weights": {...}, "actual_weights": {...},
     "risk": {...}, "gross": 1.32, "turnover": 0.06, "cost_bps": 4.1, ...}
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# <repo>/data/portfolio_runs/   (sibling-safe, survives an openalgo re-clone)
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = _REPO_ROOT / "data" / "portfolio_runs"


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
        line: Dict[str, Any] = {"ts": stamp}
        if run_id:
            line["run_id"] = run_id
        for k, v in (payload or {}).items():
            line[k] = v
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
        return path
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never abort a run
        logger.warning("portfolio run_history.record failed: %s", exc)
        return None


def read_recent(*, days: int = 14, limit: int = 500,
                directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read the most recent runs across the last ``days`` daily files (oldest first).

    Malformed lines are skipped so a torn tail write is safe.
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
            logger.debug("portfolio run_history read failed for %s: %s", path, exc)
    return rows[-limit:] if limit else rows


def latest(directory: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Return the single most recent run, or None."""
    rows = read_recent(days=7, limit=1, directory=directory)
    return rows[-1] if rows else None


def available(directory: Optional[Path] = None) -> bool:
    """True when at least one run file exists."""
    return _dir(directory).is_dir() and any(_dir(directory).glob("*.jsonl"))


def snapshots(*, days: int = 30, limit: int = 500,
              directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Runs reshaped as drift snapshots for ``attribution.drift``.

    Each element is ``{"ts", "target", "actual"}`` — directly consumable by
    ``drift.drift_series`` / ``drift.latest_drift``.
    """
    out: List[Dict[str, Any]] = []
    for row in read_recent(days=days, limit=limit, directory=directory):
        out.append({
            "ts": row.get("ts"),
            "target": row.get("target_weights") or {},
            "actual": row.get("actual_weights") or {},
        })
    return out


__all__ = ["record", "read_recent", "latest", "available", "snapshots",
           "DEFAULT_DIR"]