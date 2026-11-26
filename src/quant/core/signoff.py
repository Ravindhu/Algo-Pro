"""Owner sign-off for a live start (audit fix 4, 2026-11-26).

Flow stage: Live start gate
Status: PHASE 3 IMPLEMENTED (sign-off record check, fail closed).

A sign-off is a JSON record written by scripts/run/record_signoff.py after:
  * the safety drill (quant.monitor.drill) passed, with no order sent, and
  * the owner attests that the analyze-mode soak ran clean for enough sessions.

The live start refuses unless a recent sign-off exists AND its fingerprint matches the live
config files now on disk. Changing bot.yaml, settings.yaml, risk_limits.yaml or data_sources.yaml
therefore needs a fresh sign-off. Fail closed: a missing, unreadable or stale record refuses.

Limit: the soak count is an owner attestation, not a measured result. The record proves a
process was followed; it is not a security control against someone who can write the state folder.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

MIN_SOAK_SESSIONS = 5       # clean analyze-mode trading sessions the owner must attest to
MAX_SIGNOFF_AGE_DAYS = 7    # a sign-off older than this does not start live


def config_fingerprint(paths: Iterable) -> str:
    """SHA-256 over each config file's name and bytes, in the order given. A missing file hashes as absent."""
    h = hashlib.sha256()
    for p in paths:
        p = Path(p)
        h.update(p.name.encode("utf-8") + b"\0")
        h.update(p.read_bytes() if p.is_file() else b"<absent>")
        h.update(b"\0")
    return h.hexdigest()


def build_signoff(*, fingerprint: str, drill_ok: bool, soak_sessions: int, soak_notes: str,
                  approved_by: str, today: date) -> dict:
    """The record to save. Refuses to build one that could never pass (drill failed, too few sessions)."""
    if not drill_ok:
        raise ValueError("sign-off refused: the safety drill did not pass")
    if not isinstance(soak_sessions, int) or soak_sessions < MIN_SOAK_SESSIONS:
        raise ValueError(f"sign-off refused: need at least {MIN_SOAK_SESSIONS} clean analyze sessions")
    if not str(approved_by or "").strip():
        raise ValueError("sign-off refused: approved_by is empty")
    return {
        "date": today.isoformat(),
        "config_fingerprint": fingerprint,
        "drill_passed": True,
        "soak_sessions": int(soak_sessions),
        "soak_notes": str(soak_notes or ""),
        "approved_by": str(approved_by).strip(),
    }


def save_signoff(path, record: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


def check_signoff(path, *, fingerprint: str, today: date,
                  max_age_days: int = MAX_SIGNOFF_AGE_DAYS,
                  min_sessions: int = MIN_SOAK_SESSIONS) -> Tuple[bool, List[str]]:
    """(ok, reasons). Every failure is a reason; any reason refuses the live start."""
    p = Path(path)
    if not p.is_file():
        return False, ["no owner sign-off recorded (run scripts/run/record_signoff.py)"]
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        when = date.fromisoformat(str(rec["date"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False, ["owner sign-off file unreadable: refusing to assume it is valid"]
    reasons: List[str] = []
    if rec.get("config_fingerprint") != fingerprint:
        reasons.append("sign-off is for different config files: sign off again after any config change")
    if rec.get("drill_passed") is not True:
        reasons.append("sign-off has no passed safety drill")
    sessions = rec.get("soak_sessions")
    if not isinstance(sessions, int) or isinstance(sessions, bool) or sessions < min_sessions:
        reasons.append(f"sign-off has fewer than {min_sessions} clean analyze sessions")
    if not str(rec.get("approved_by") or "").strip():
        reasons.append("sign-off has no approver")
    age = (today - when).days
    if age < 0:
        reasons.append("sign-off date is in the future: refusing")
    elif age > max_age_days:
        reasons.append(f"sign-off is {age} days old (max {max_age_days}): sign off again")
    return not reasons, reasons
