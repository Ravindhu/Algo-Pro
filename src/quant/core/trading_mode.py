"""Trading mode for the dashboard: PAPER or LIVE (decisions 0028, 0030; approval removed 2026-11-25).

Flow stage: Dashboard
Status: live selectable. No LiveApproval (removed 2026-11-25).

THE RULE (2026-11-25)
---------------------
The mode is a record of what the operator WANTS. It is never the authority to place
an order. Live orders are sent only when OpenAlgo's analyzer (sandbox) is OFF
(``execution/openalgo_client.py``), and every intent still needs an approved
GateDecision from the risk gate.

Reads are fail-safe: a missing or corrupt file means paper, because a file that
cannot be read must never look like live.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

MODES = ("paper", "live")

#: Repo-relative default. Resolved from this file so nothing depends on cwd.
DEFAULT_STATE_PATH = Path("data") / "mode.json"


def _paths(state_path=None):
    """Resolve the repo-rooted default against the caller's override (for tests)."""
    root = Path(__file__).resolve().parents[3]
    return Path(state_path) if state_path is not None else root / DEFAULT_STATE_PATH


def _read_state(state_path: Path) -> Dict[str, Any]:
    """Read mode.json. Missing or unreadable means paper: never look live by accident."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mode": "paper"}
    mode = data.get("mode")
    return data if mode in MODES else {"mode": "paper"}


def trading_mode_status(state_path=None, **_ignored) -> Dict[str, object]:
    """The mode as the dashboard should show it.

    ``mode`` is what is requested and ``effective_mode`` is the same value. Whether an order
    reaches the broker is decided by OpenAlgo's analyzer switch at send time.
    """
    state = _paths(state_path)
    requested = _read_state(state).get("mode", "paper")
    return {
        "mode": requested,
        "effective_mode": requested,
        "live_allowed": requested == "live",
        "approver": None,
        "live_check": "OpenAlgo analyzer must be off to send live orders",
    }


def request_mode(mode: Optional[str], *, state_path=None, audit=None,
                 **_ignored) -> Tuple[bool, str]:
    """(accepted, message). Paper and live are both accepted; every request is audited."""
    state = _paths(state_path)
    if mode not in MODES:
        if audit is not None:
            audit.append("mode_change_refused", {"requested": mode,
                                                 "reason": "invalid mode"})
        return False, "Mode must be paper or live."

    message = ("Live mode selected. Orders still stay in the OpenAlgo sandbox while its analyzer is on."
               if mode == "live" else "Paper mode selected.")
    state.parent.mkdir(parents=True, exist_ok=True)
    tmp = state.with_suffix(state.suffix + ".tmp")
    tmp.write_text(json.dumps({"mode": mode}, indent=2), encoding="utf-8")
    tmp.replace(state)
    if audit is not None:
        audit.append("mode_changed", {"mode": mode})
    return True, message
