"""Trading mode for the dashboard: PAPER or LIVE (decision 0028).

PAPER can be selected. LIVE is always refused here. Live orders need a LiveApproval with an
approver, a matching limits hash and an approved GateDecision (decision 0017; the guard is in
the execution package (decision 0017)). The dashboard never creates those, so it cannot switch live on.
The mode is not saved to a file: paper is the only mode the dashboard can select, so a restart returns to paper.

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (paper selectable; live refused)
"""
from typing import Dict, Optional, Tuple

MODES = ("paper", "live")
LIVE_REFUSAL = (
    "Live is refused: it needs a LiveApproval (approver, limits hash, approved gate decision). "
    "The dashboard cannot create one (decision 0017, decision 0028)."
)


def trading_mode_status() -> Dict[str, object]:
    return {"mode": "paper", "live_allowed": False, "reason": LIVE_REFUSAL}


def request_mode(mode: Optional[str]) -> Tuple[bool, str]:
    """(accepted, message). Paper is accepted; live is refused; anything else is invalid."""
    if mode == "paper":
        return True, "Paper mode selected."
    if mode == "live":
        return False, LIVE_REFUSAL
    return False, "Mode must be paper or live."
