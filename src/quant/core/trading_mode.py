"""Trading mode (decisions 0028 and 0031): one switch, on the NSE dashboard.

PAPER is always accepted. LIVE is accepted only when all three hold:
  1. the confirm phrase GO LIVE is typed,
  2. a named approver is given,
  3. every check in core/live_checklist.py passes.
The mode lives in memory only. A restart returns to PAPER. This module is the only place that
writes the mode (a test checks this). Nothing here places an order.

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (live switch with checklist; live refused until a licensed live data source exists)
"""
from typing import Optional, Sequence, Tuple

from quant.core.live_checklist import Check, allowed

MODES = ("paper", "live")
CONFIRM_PHRASE = "GO LIVE"
_current = "paper"


def current_mode() -> str:
    return _current


def trading_mode_status() -> dict:
    return {"mode": _current, "modes": list(MODES), "confirm_phrase": CONFIRM_PHRASE,
            "live_needs": ["confirm phrase", "named approver", "all 8 live checks (decision 0031)"]}


def request_mode(mode: Optional[str], *, checks: Optional[Sequence[Check]] = None,
                 confirm: Optional[str] = None, approver: Optional[str] = None) -> Tuple[bool, str]:
    """(accepted, message). Only this function changes the mode."""
    global _current
    if mode == "paper":
        _current = "paper"
        return True, "Paper mode selected."
    if mode == "live":
        if confirm != CONFIRM_PHRASE:
            return False, f"Live needs the confirm phrase {CONFIRM_PHRASE!r}."
        if not approver or not str(approver).strip():
            return False, "Live needs a named approver."
        if checks is None:
            return False, "Live refused: the live checklist was not run."
        failing = [c for c in checks if not c.passed]
        if failing or not allowed(checks):
            detail = "; ".join(f"{c.name}: {c.reason}" for c in failing) or "checklist incomplete"
            return False, "Live refused: " + detail
        _current = "live"
        return True, "Live mode selected."
    return False, "Mode must be paper or live."
