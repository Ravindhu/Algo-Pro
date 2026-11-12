"""Agent recommendations store: the pipeline writes, the dashboard reads.

Plan: docs/IMPLEMENTATION_PLAN_ACCOUNTS_AND_RECOMMENDATIONS.md (Part B step 1).

One file: data/paper/recommendations.json (runtime state, git-ignored).
Each entry:
    id          stable id (hash of station+parameter), so the apply route
                accepts ONLY an id - the browser never sends a number
    station     the flow stage that produced it
    parameter   the risk-limit / setup key
    current     the value the recommendation was made against
    recommended the agent's calculated value (advice, never auto-applied)
    reason      short text for the popup
    created     unix seconds
    expires     unix seconds (default 1 hour after creation)

Fail-closed rules: unreadable file = no recommendations; writes are atomic;
expired entries are dropped on write and refused on apply. Nothing here can
place an order; applying is a separate, owner-only dashboard route.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = ROOT / "data" / "paper" / "recommendations.json"
DEFAULT_TTL_S = 3600  # 1 hour; a stale recommendation must not be applied

_FIELDS = {"id", "station", "parameter", "current", "recommended",
           "reason", "created", "expires"}


def _read(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # unreadable file: no recommendations, fail closed
    recs = data.get("recommendations") if isinstance(data, dict) else None
    return recs if isinstance(recs, dict) else {}


def _write(path: Path, recs: Dict[str, Dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".recs-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"recommendations": recs}, stream, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def put(station: str, parameter: str, current: Any, recommended: Any,
        reason: str, path: Path = DEFAULT_PATH, *,
        now: Optional[float] = None, ttl_s: int = DEFAULT_TTL_S) -> Dict[str, Any]:
    """Store (or refresh) one recommendation. Returns the entry."""
    import hashlib
    import time

    stamp = time.time() if now is None else float(now)
    rec_id = hashlib.sha256(f"{station}|{parameter}".encode("utf-8")).hexdigest()[:16]
    entry = {
        "id": rec_id,
        "station": str(station)[:40],
        "parameter": str(parameter)[:64],
        "current": current,
        "recommended": recommended,
        "reason": str(reason or "")[:200],
        "created": stamp,
        "expires": stamp + max(1, int(ttl_s)),
    }
    recs = _read(path)
    recs[rec_id] = entry
    _write(path, _drop_expired(recs, stamp))
    return entry


def get(rec_id: str, path: Path = DEFAULT_PATH, *,
        now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """One live recommendation by id, or None (unknown or expired)."""
    import time

    stamp = time.time() if now is None else float(now)
    entry = _read(path).get(str(rec_id))
    if not entry or entry.get("expires", 0) <= stamp:
        return None
    return entry


def remove(rec_id: str, path: Path = DEFAULT_PATH) -> bool:
    """Drop one entry. True if it existed; the apply route uses this so a
    double-click cannot apply the same recommendation twice."""
    recs = _read(path)
    if str(rec_id) not in recs:
        return False
    recs.pop(str(rec_id), None)
    _write(path, recs)
    return True


def live(path: Path = DEFAULT_PATH, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """All unexpired recommendations, oldest first (popup order)."""
    import time

    stamp = time.time() if now is None else float(now)
    recs = [e for e in _read(path).values()
            if isinstance(e, dict) and e.get("expires", 0) > stamp and _FIELDS <= set(e)]
    recs.sort(key=lambda e: e.get("created", 0))
    return recs


def _drop_expired(recs: Dict[str, Dict[str, Any]], stamp: float) -> Dict[str, Dict[str, Any]]:
    return {k: v for k, v in recs.items() if v.get("expires", 0) > stamp}
