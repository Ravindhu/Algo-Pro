"""Map a 6-digit BSE code to its OpenAlgo symbol, then check the broker knows it.

Status: PHASE 3 IMPLEMENTED read-only (tests in tests/unit/test_bse_symbol.py). Plan:
docs/IMPLEMENTATION_PLAN_2026-11-25_BSE_SYMBOL_MAP.md

Read only. Nothing here places, cancels or modifies an order, and the screen
file is not changed. ``tradable`` is always False: the bot trades NSE only.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional

_BSE_CODE_RE = re.compile(r"^\d{6}$")

#: ``lookup(code) -> OpenAlgo symbol or None``
CodeLookup = Callable[[str], Optional[str]]
#: ``reader(symbol, exchange) -> raw OpenAlgo quotes reply``
QuoteReader = Callable[[str, str], Dict[str, Any]]


def is_bse_code(text: Any) -> bool:
    """True only for exactly six digits (BSE security codes)."""
    return isinstance(text, str) and bool(_BSE_CODE_RE.match(text.strip()))


def openalgo_bse_lookup(code: str) -> Optional[str]:
    """OpenAlgo symbol for a BSE token, or None. Never raises.

    Uses OpenAlgo's own symbol master, the same in-process import as engines/core/lot.py.
    Outside the OpenAlgo runtime the import fails and the answer is None (fail closed).
    """
    try:
        from database.token_db_enhanced import get_symbol  # type: ignore
        symbol = get_symbol(str(code), "BSE")  # token (BSE code) -> OpenAlgo symbol
    except Exception:  # noqa: BLE001 - a missing master must not stop the screen
        return None
    return symbol if isinstance(symbol, str) and symbol else None


def _broker_knows(reply: Any) -> bool:
    return isinstance(reply, dict) and reply.get("status") == "success" \
        and isinstance(reply.get("data"), dict)


def bse_report(candidates: Iterable[Dict[str, Any]], lookup: CodeLookup,
               quote_reader: Optional[QuoteReader]) -> List[Dict[str, Any]]:
    """One row per 6-digit candidate: code, OpenAlgo symbol, broker match, reason.

    Non-numeric symbols are ignored. ``tradable`` is always False in this change.
    """
    rows: List[Dict[str, Any]] = []
    for cand in candidates or []:
        code = str((cand or {}).get("symbol") or "").strip()
        if not is_bse_code(code):
            continue
        try:
            symbol = lookup(code)
        except Exception:  # noqa: BLE001
            symbol = None
        if not symbol:
            rows.append({"code": code, "symbol": None, "broker_match": False,
                         "reason": "code not in OpenAlgo symbol master", "tradable": False})
            continue
        if quote_reader is None:
            rows.append({"code": code, "symbol": symbol, "broker_match": False,
                         "reason": "no broker quote source", "tradable": False})
            continue
        try:
            reply = quote_reader(symbol, "BSE")
        except Exception:  # noqa: BLE001
            reply = None
        matched = _broker_knows(reply)
        rows.append({"code": code, "symbol": symbol, "broker_match": matched,
                     "reason": ("broker knows this BSE symbol; bot is NSE-only, not tradable"
                                if matched else "broker did not return a BSE quote"),
                     "tradable": False})
    return rows
