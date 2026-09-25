"""
reverse_exit.py - Intraday reverse-exit blocking (Feature 8).

Responsibility
--------------
Implement the compliance guard for delivery-restricted scrips
(``T2T`` / ``BE`` / ``ST`` series on NSE). If a POV execution is in
its accumulation (BUY) phase, the slicer must NOT route a same-day
reverse exit (SELL) on the same script without an explicit supervisor
override.

The blocker is also useful for any scrip where the user has flagged
``restricted_same_day_reverse_exit`` manually in the JSON config.

Two helpers
-----------
* :func:`same_day_reverse_exit_blocked` - the predicate the slicer
  calls before letting a child order through. ``True`` means
  "do not place, return a structured error".
* :func:`resolve_restricted_flag` - turns the per-symbol symbol_info
  record and the JSON config into a single boolean the slicer can
  attach to its execution state.

The module is pure (no SDK, no DB, no network) so it is unit-testable
in isolation and reusable by any future exit-phase algorithm.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from blue_engine.core.symbol_meta import is_restricted_same_day_reverse_exit
from blue_engine.core.time_utils import ist_now


def resolve_restricted_flag(
    symbol_info: Any,
    request_data: dict[str, Any],
) -> bool:
    """Combine symbol-master detection and config override.

    Resolution order (highest priority first):

      1. ``request_data["restricted_same_day_reverse_exit"]`` - an
         explicit boolean from the JSON config. ``True`` forces the
         block on, ``False`` forces it off.
      2. :func:`is_restricted_same_day_reverse_exit` against the
         provided symbol_info. ``True`` when the master says the
         series is T2T/BE/ST.
      3. ``False`` when neither source says so.
    """
    explicit = request_data.get("restricted_same_day_reverse_exit")
    if explicit is not None:
        return bool(explicit)
    return is_restricted_same_day_reverse_exit(symbol_info)


def same_day_reverse_exit_blocked(
    request_data: dict[str, Any],
    now: datetime | None = None,
    last_fill_trade_date: Optional[str] = None,
    intent_action: str | None = None,
    current_phase: str | None = None,
    supervisor_override: bool = False,
) -> bool:
    """Return ``True`` if this child order is a same-day reverse exit.

    The predicate is structured so a single line of code in the POV
    loop can gate the slice. It combines three inputs:

      1. Whether the symbol is in a delivery-restricted series
         (resolved by :func:`resolve_restricted_flag`).
      2. Whether the *current* attempt is genuinely a reverse exit
         (i.e. the slicer is in an accumulation phase, or the
         intent is the opposite side of the original action, AND
         the trade date matches the original fill date).
      3. Whether a supervisor override is in effect - if so, the
         block is released and the call returns ``False``.

    Args:
        request_data:        Slicer config dict.
        now:                 Reference time (defaults to IST now).
        last_fill_trade_date: ISO date (``"YYYY-MM-DD"``) of the
                              last fill of the run. ``None`` is
                              treated as "no prior fill today".
        intent_action:       The action the slicer is about to
                              submit (``"BUY"`` / ``"SELL"``). When
                              ``None``, the call defaults to the
                              action stored in ``request_data``.
        current_phase:       ``"accumulating"`` or ``"exiting"``.
        supervisor_override: ``True`` if a human supervisor has
                              explicitly approved the reverse exit.

    Returns:
        ``True`` if the order must be blocked. ``False`` when the
        order is safe to send.
    """
    if supervisor_override:
        return False

    is_restricted = resolve_restricted_flag(None, request_data)
    if not is_restricted:
        return False

    current = now or ist_now()
    trade_date = last_fill_trade_date or request_data.get("execution_trade_date")
    if trade_date is None:
        # No fill yet -> nothing to reverse -> block is irrelevant.
        return False
    if current.date().isoformat() != trade_date:
        # Different day -> the intraday rule does not apply.
        return False

    action = (intent_action or request_data.get("action", "")).upper()
    original = str(request_data.get("action", "")).upper()
    if not action or not original:
        return False
    phase = (current_phase or "accumulating").lower()
    if phase == "exiting":
        # The exit phase itself is always allowed to send its
        # reverse-side orders; the rule only blocks ad-hoc
        # SELL-on-the-same-script during the accumulation phase.
        return False
    return action != original


__all__ = [
    "resolve_restricted_flag",
    "same_day_reverse_exit_blocked",
]
