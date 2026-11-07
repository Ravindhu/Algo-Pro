"""Retry for square-off exits, with a safety check before every retry.

Flow stage: Square-off and rollover, Order state machine
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none (new, flow item 6)

Why the check: a timeout can hide an order that the broker did receive. Sending again could
then close the position twice and open a short. So a retry happens only when BOTH readers
confirm it is safe:
- order_not_placed(intent): True when the broker has no open order tagged with this intent.
- position_still_open(intent): True when the position still needs the exit.
If either reader is missing, or confirms something else, there is no retry. A CRITICAL alert is raised.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional

RETRY_STATUS = "UNKNOWN"


def retry_exit(
    send_once: Callable[[object, object], Dict],
    intent,
    decision,
    *,
    order_not_placed: Optional[Callable[[object], bool]] = None,
    position_still_open: Optional[Callable[[object], bool]] = None,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    alerts=None,
) -> Dict:
    result = send_once(intent, decision)
    if result.get("status") != RETRY_STATUS or result.get("order_id"):
        return result  # a definite answer, or the order exists: never resend
    readers_ok = order_not_placed is not None and position_still_open is not None
    for attempt in range(1, attempts):
        if not readers_ok:
            break
        if not order_not_placed(intent) or not position_still_open(intent):
            break
        sleep(2 ** attempt)
        result = send_once(intent, decision)
        if result.get("status") != RETRY_STATUS or result.get("order_id"):
            return result
    if alerts is not None:
        alerts.emit("CRITICAL", "square-off", f"exit {intent.intent_id} for {intent.symbol} not confirmed; check the position by hand")
    return result
