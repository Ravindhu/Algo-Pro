"""Margin-funded (MTF) borrowed-amount check. Pure, fail closed, OFF by default.

Flow stage: Risk gate (margin funding)
Status: PHASE 1 IMPLEMENTED (risk check only)
Not wired into any order path. No broker product for a margin order is sent: the OpenAlgo
schemas accept only MIS, NRML and CNC, and the broker's margin product and account rules are
unconfirmed (mStock). Enabling the flag alone places no margin order.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple


def _bad(value) -> bool:
    return isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0


def margin_exposure_check(enabled: bool, limit_inr: Optional[float],
                          borrowed_now_inr: float, new_borrow_inr: float) -> Tuple[bool, str]:
    """(allowed, reason). Refuses unless margin funding is on, a limit is set, and the total fits it."""
    if enabled is not True:
        return False, "margin funding is off (settings margin.mtf_enabled is not true)"
    if limit_inr is None or _bad(limit_inr) or limit_inr == 0:
        return False, "no borrowed-amount limit set: refused"
    if _bad(borrowed_now_inr) or _bad(new_borrow_inr):
        return False, "bad borrowed amount: refused"
    total = float(borrowed_now_inr) + float(new_borrow_inr)
    if total > float(limit_inr):
        return False, f"would borrow {total:.2f}, above the limit {float(limit_inr):.2f}"
    return True, "ok"
