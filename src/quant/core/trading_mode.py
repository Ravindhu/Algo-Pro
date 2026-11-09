"""Trading mode for the dashboard: PAPER or LIVE (decisions 0028, 0030).

Flow stage: Dashboard
Status: PHASE 4 IMPLEMENTED (live selectable WITH a valid LiveApproval)

THE ONE RULE (decision 0030)
----------------------------
The mode is a record of what the operator WANTS. It is never the authority to
place an order. Authority lives in two other places:

* a ``LiveApproval`` written by a person (``core/live_approval.py``), and
* the risk gate, which must produce an approved ``GateDecision`` per intent.

Switching to LIVE without a valid approval is refused and audited, so a
mis-click does nothing except write an audit row.

WHY THE MODE IS PERSISTED NOW (a change from decision 0028)
-----------------------------------------------------------
0028 kept the mode in memory only, because paper was the only selectable mode
and a restart should not silently carry authority across a boot. That reasoning
still holds for AUTHORITY - and the approval file is what carries authority -
but the mode itself is now meaningful state: the dashboard header, the paper
loop and ``run_live.py`` must all report and act on the SAME mode, so it lives in
one file that is re-validated on every read.

Reads are fail-safe: a missing or corrupt file means paper, because a file that
cannot be read must never look like live.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from quant.core.live_approval import check_approval

MODES = ("paper", "live")

#: Repo-relative default. Resolved from this file so nothing depends on cwd.
DEFAULT_STATE_PATH = Path("data") / "mode.json"
DEFAULT_APPROVAL_PATH = Path("data") / "live_approval.json"
DEFAULT_LIMITS_PATH = Path("config") / "risk_limits.yaml"

LIVE_REFUSAL_NO_APPROVAL = (
    "Live refused: no valid live approval on file. "
    "A person must run scripts/ops/approve_live.py first (decision 0030)."
)


def _paths(state_path=None, approval_path=None, limits_path=None):
    """Resolve repo-rooted defaults against the caller's overrides (for tests)."""
    root = Path(__file__).resolve().parents[3]
    state = Path(state_path) if state_path is not None else root / DEFAULT_STATE_PATH
    approval = (Path(approval_path) if approval_path is not None
                else root / DEFAULT_APPROVAL_PATH)
    limits = Path(limits_path) if limits_path is not None else root / DEFAULT_LIMITS_PATH
    return state, approval, limits


def _read_state(state_path: Path) -> Dict[str, Any]:
    """Read mode.json. Missing or unreadable means paper: never look live by accident."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mode": "paper"}
    mode = data.get("mode")
    return data if mode in MODES else {"mode": "paper"}


def trading_mode_status(state_path=None, approval_path=None,
                        limits_path=None) -> Dict[str, object]:
    """The mode as the dashboard should show it, with the live check included.

    ``mode`` is what is requested. ``effective_mode`` is what is actually
    armed: paper whenever the approval has lapsed or the limits changed, so the
    header never claims live that the runner would refuse.
    """
    state, approval, limits = _paths(state_path, approval_path, limits_path)
    data = _read_state(state)
    requested = data.get("mode", "paper")
    status: Dict[str, object] = {
        "mode": requested,
        "effective_mode": requested,
        "live_allowed": False,
        "approver": None,
        "live_check": None,
    }
    if requested != "live":
        return status

    ok, reason = check_approval(approval, limits)
    status["live_check"] = reason
    if not ok:
        # The approval is gone or stale: the honest answer is paper.
        status["effective_mode"] = "paper"
        status["reason"] = reason
        return status
    status["live_allowed"] = True
    status["approver"] = json.loads(approval.read_text(encoding="utf-8")).get("approver")
    return status


def request_mode(mode: Optional[str], *, state_path=None, approval_path=None,
                 limits_path=None, audit=None) -> Tuple[bool, str]:
    """(accepted, message). Paper always accepted; live only with a valid approval.

    Every request is audited so the log shows who tried to flip the switch and
    whether it worked, including the refusals.
    """
    state, approval, limits = _paths(state_path, approval_path, limits_path)

    if mode not in MODES:
        if audit is not None:
            audit.append("mode_change_refused", {"requested": mode,
                                                 "reason": "invalid mode"})
        return False, "Mode must be paper or live."

    if mode == "live":
        ok, reason = check_approval(approval, limits)
        if not ok:
            if audit is not None:
                audit.append("mode_change_refused",
                             {"requested": "live", "reason": reason})
            return False, f"Live refused: {reason}"
        message = f"Live mode armed. {reason}"

    if mode == "paper":
        message = "Paper mode selected."

    state.parent.mkdir(parents=True, exist_ok=True)
    tmp = state.with_suffix(state.suffix + ".tmp")
    tmp.write_text(json.dumps({"mode": mode}, indent=2), encoding="utf-8")
    tmp.replace(state)
    if audit is not None:
        audit.append("mode_changed", {"mode": mode})
    return True, message
