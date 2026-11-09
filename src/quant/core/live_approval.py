"""LiveApproval record: the human sign-off that unlocks live trading (decision 0030).

Flow stage: Risk gate
Status: PHASE 4 IMPLEMENTED (approval record only; it authorises nothing by itself)

WHAT THIS IS
------------
A small JSON file, written by a PERSON at a terminal via
``scripts/ops/approve_live.py``. It records who approved live trading, when, why,
and the SHA-256 of the exact ``config/risk_limits.yaml`` they reviewed.

It exists so that "switch the dashboard to LIVE" is two separate acts:
  1. a human reads the limits and records an approval,
  2. the operator flips the switch.

One act alone does nothing.

WHY THE HASH MATTERS
--------------------
An approval is only meaningful for the limits the approver actually saw. Editing
any limit changes the file hash, so the approval stops matching and the system
falls back to paper. Without this, a person could approve a 10-share cap and
someone could later raise it to 10,000 while the old approval silently stayed
valid. The same check is already enforced at order time by
``execution/openalgo_client.LiveApproval``; this module is where the record is
created and verified, not a second opinion.

WHAT IT DOES NOT DO
-------------------
It does not place, size or authorise an order. Holding a valid approval only
means live mode MAY be selected. Every order still needs an approved
``GateDecision`` whose ``intent_id`` matches, from the risk gate.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: The phrase an operator must type. Deliberately not a yes/no prompt: a stray
#: Enter must not approve live trading.
APPROVAL_PHRASE = "I APPROVE LIVE TRADING"

#: An approval older than this is treated as stale and refuses live mode.
#: A sign-off is a statement about today's limits and today's system, not a
#: permanent licence.
MAX_AGE_DAYS = 30


def file_sha256(path: Path) -> str:
    """SHA-256 of a file as hex. Ties an approval to one exact limits file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class LiveApprovalRecord:
    approver: str
    approved_on: str           # ISO date
    reason: str
    limits_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approver": self.approver,
            "approved_on": self.approved_on,
            "reason": self.reason,
            "limits_sha256": self.limits_sha256,
        }


def create_approval(path: Path, *, approver: str, reason: str,
                    limits_path: Path, phrase: str,
                    today: Optional[date] = None) -> LiveApprovalRecord:
    """Write an approval file. Refuses unless a person typed the exact phrase.

    Raises ``ValueError`` on a bad phrase, a blank approver or reason, or a
    missing limits file. It never invents an approver: a blank name means the
    call is wrong, not that an anonymous approval is acceptable.
    """
    if phrase != APPROVAL_PHRASE:
        raise ValueError("approval refused: the phrase was not typed exactly")
    if not approver or not approver.strip():
        raise ValueError("approval refused: an approver name is required")
    if not reason or not reason.strip():
        raise ValueError("approval refused: a reason is required")
    limits_path = Path(limits_path)
    if not limits_path.is_file():
        raise ValueError(f"approval refused: limits file not found at {limits_path}")

    record = LiveApprovalRecord(
        approver=approver.strip(),
        approved_on=(today or datetime.now(timezone.utc).date()).isoformat(),
        reason=reason.strip(),
        limits_sha256=file_sha256(limits_path),
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return record


def load_approval(path: Path) -> Optional[LiveApprovalRecord]:
    """Read the approval file, or None when absent or unreadable. Never raises."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LiveApprovalRecord(
            approver=str(data["approver"]),
            approved_on=str(data["approved_on"]),
            reason=str(data["reason"]),
            limits_sha256=str(data["limits_sha256"]),
        )
    except KeyError:
        return None


def check_approval(approval_path: Path, limits_path: Path,
                   today: Optional[date] = None,
                   max_age_days: int = MAX_AGE_DAYS) -> Tuple[bool, str]:
    """(valid, reason). Every failure names what is wrong, for the dashboard.

    Fails closed on: no file, unreadable file, blank approver, a limits hash
    that no longer matches, and an approval older than ``max_age_days``.
    """
    record = load_approval(approval_path)
    if record is None:
        return False, ("no live approval on file; a person must run "
                       "scripts/ops/approve_live.py first")
    if not record.approver.strip():
        return False, "live approval has no approver name"

    limits_path = Path(limits_path)
    if not limits_path.is_file():
        return False, f"limits file not found at {limits_path}"
    if record.limits_sha256 != file_sha256(limits_path):
        return False, ("risk_limits.yaml changed since approval; the approval is void "
                       "and live mode falls back to paper")

    try:
        approved = date.fromisoformat(record.approved_on)
    except ValueError:
        return False, f"approval date {record.approved_on!r} is not an ISO date"
    age = ((today or datetime.now(timezone.utc).date()) - approved).days
    if age < 0:
        return False, "approval is dated in the future"
    if age > max_age_days:
        return False, (f"approval is {age} days old (max {max_age_days}); "
                       "re-approve the current limits")

    return True, f"live approved by {record.approver} on {record.approved_on}"


def revoke_approval(path: Path) -> bool:
    """Delete the approval file. Returns True when something was removed."""
    path = Path(path)
    if path.exists():
        path.unlink()
        return True
    return False
