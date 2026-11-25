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


def load_eligible(path) -> frozenset:
    """Margin-delivery eligible symbols from a plain list (one symbol per line; '#' comments and a
    'symbol' header allowed). A missing or unreadable file gives an empty set, so nothing is eligible."""
    import pathlib
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return frozenset()
    names = set()
    for line in text.splitlines():
        token = line.split("#", 1)[0].strip().split(",", 1)[0].strip().upper()
        if token and token != "SYMBOL":
            names.add(token)
    return frozenset(names)


def eligibility_check(symbol: str, eligible: frozenset) -> Tuple[bool, str]:
    """(allowed, reason). Only symbols on the eligible list may use margin. An empty list refuses all."""
    if not isinstance(symbol, str) or not symbol.strip():
        return False, "no symbol: refused"
    if not eligible:
        return False, "margin-eligible list is empty or missing: refused"
    if symbol.strip().upper() not in eligible:
        return False, f"{symbol.strip().upper()} is not on the margin-delivery eligible list"
    return True, "ok"
