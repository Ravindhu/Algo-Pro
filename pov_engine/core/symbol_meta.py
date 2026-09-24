"""
symbol_meta.py - SME / Periodic Call Auction (PCAS) and security-segment
detection from broker symbol-master metadata.

Responsibility
--------------
A single, broker-agnostic way to look at a symbol's master record and
answer two questions the POV loop needs to stay compliant on non-
continuous listings:

  1. Is the instrument flagged as an SME or a Periodic Call Auction
     listing? If so, the engine must disable continuous streaming
     orders and only place during configured call-auction windows.
     (Feature 5 - SME / Periodic Call Auction Support)

  2. Does the symbol belong to a delivery-restricted series (T2T /
     BE / ST) where intraday reverse exits on the same trade date
     are forbidden? (Feature 8 - Intraday Reverse Exit Blocking)

This module is *pure* - it does not import the SDK, does not call
the network, and does not touch the database. Callers pass in a
symbol_info record (either a SQLAlchemy SymbolData instance, a
plain dict, or anything with the right attribute names) and a
helper here classifies it. Keeping it pure makes it trivially
unit-testable and reusable by any future slicer (TWAP, VWAP,
Iceberg, etc.) without code duplication.

Design rules (mirrored from pov_engine/core/time_utils.py)
- No SDK calls. No network. No DB. Pure helpers.
- Graceful degradation: a None or empty symbol_info always answers
  ``False`` rather than raising. The slicer must keep running.
- All detection is broker-agnostic. The same regex set works for
  the NSE/BSE master format the OpenAlgo SDK normalises to.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Tiny field-access helper
# ---------------------------------------------------------------------------
def _metadata_value(info: Any, *names: str) -> str:
    """Return the first non-empty string value among ``names``.

    Supports both attribute-style (SQLAlchemy ORM rows) and mapping-style
    (plain dicts / ``dataclasses.asdict``) records. Returns an empty
    string when no match is found, never ``None``, so downstream
    regex matching never has to special-case the absent case.

    Args:
        info:  The symbol record (object or dict).
        names: Candidate attribute / key names, tried in order.

    Returns:
        The first non-empty value, stripped of surrounding whitespace.
        An empty string when the field is absent or blank.
    """
    for name in names:
        if isinstance(info, dict):
            value = info.get(name)
        else:
            value = getattr(info, name, None)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


# ---------------------------------------------------------------------------
# Public API - segment classification
# ---------------------------------------------------------------------------
def detect_restricted_segment(symbol_info: Any) -> str | None:
    """Return the T2T / BE / ST series tag if the symbol is restricted.

    Brokers encode the series in several places: the ``segment`` field,
    the ``instrumenttype`` field, the ``series`` field, or as a suffix
    on the trading symbol itself (``INFY-BE`` or ``INFYBE``). We try
    every common location and return the first tag we find.

    Args:
        symbol_info: A symbol-master record (ORM row or dict).

    Returns:
        One of ``"T2T"``, ``"BE"``, ``"ST"`` as a canonical string,
        or ``None`` if the symbol is not in a restricted series.

    Examples:
        >>> detect_restricted_segment({"symbol": "INFY-BE"})
        'BE'
        >>> detect_restricted_segment({"segment": "T2T"})
        'T2T'
        >>> detect_restricted_segment({"symbol": "INFY", "segment": "EQ"})
    """
    if symbol_info is None:
        return None

    candidates: Iterable[str] = (
        _metadata_value(symbol_info, "segment", "series", "security_segment", "security_type"),
        _metadata_value(symbol_info, "instrumenttype"),
        _metadata_value(symbol_info, "symbol", "brsymbol", "name"),
    )
    restricted_re = r"(?:^|[^A-Z0-9])(T2T|BE|ST)(?:$|[^A-Z0-9])"

    for raw in candidates:
        upper = raw.upper().replace("_", " ").replace("-", " ")
        tokens = set(upper.split())
        if upper in RESTRICTED_SAME_DAY_SEGMENTS or tokens & RESTRICTED_SAME_DAY_SEGMENTS:
            if upper in RESTRICTED_SAME_DAY_SEGMENTS:
                return upper
            return next(token for token in RESTRICTED_SAME_DAY_SEGMENTS if token in tokens)
        match = re.search(restricted_re, upper)
        if match:
            return match.group(1)
    return None


def is_restricted_same_day_reverse_exit(symbol_info: Any) -> bool:
    """Convenience predicate: ``True`` if the symbol is in a restricted series.

    Used by the slicer to short-circuit the same-day reverse-exit path
    (Feature 8). Equivalent to ``detect_restricted_segment(...) is not None``.
    """
    return detect_restricted_segment(symbol_info) is not None


import re
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Segment vocabulary
# ---------------------------------------------------------------------------
# NSE delivery-restricted series where a same-day buy-then-sell on the
# same scrip is treated as a wash-sale / cross-trade. The pattern is
# intentionally conservative: only series we have hard evidence for.


# ---------------------------------------------------------------------------
# Public API - SME / PCAS classification
# ---------------------------------------------------------------------------
def is_sme_periodic_call_auction(symbol_info: Any) -> bool:
    """Return ``True`` if the symbol is an SME or PCAS listing.

    Brokers identify these instruments in different ways:

      * NSE master: ``instrumenttype == "SME"`` or ``segment == "SME"``.
      * BSE master: ``instrumenttype == "SME"`` or symbol suffix.
      * Periodic Call Auction: the segment name itself or a token like
        ``"PERIODIC CALL AUCTION"`` somewhere in the metadata.

    We accept any of these, so the slicer does not need to know which
    broker it is talking to.

    Args:
        symbol_info: A symbol-master record (ORM row or dict).

    Returns:
        ``True`` if the symbol is flagged as SME or PCAS, ``False``
        otherwise. Always ``False`` when ``symbol_info`` is ``None``.
    """
    if symbol_info is None:
        return False
    values = (
        _metadata_value(symbol_info, "segment", "series", "security_segment"),
        _metadata_value(symbol_info, "instrumenttype"),
        _metadata_value(symbol_info, "name", "symbol", "brsymbol"),
    )
    for value in values:
        upper = value.upper()
        if not upper:
            continue
        if _SME_TOKEN_RE.search(upper) or _PCAS_TOKEN_RE.search(upper):
            return True
    return False


# ---------------------------------------------------------------------------
# Public alias
# ---------------------------------------------------------------------------
def is_sme_symbol(symbol_info: Any) -> bool:
    """Short alias used by integrations that already have symbol metadata.

    Identical to :func:`is_sme_periodic_call_auction`; kept as a separate
    name because some callers (and tests) prefer the shorter spelling.
    """
    return is_sme_periodic_call_auction(symbol_info)


__all__ = [
    "RESTRICTED_SAME_DAY_SEGMENTS",
    "detect_restricted_segment",
    "is_restricted_same_day_reverse_exit",
    "is_sme_periodic_call_auction",
    "is_sme_symbol",
]

RESTRICTED_SAME_DAY_SEGMENTS: frozenset[str] = frozenset({"T2T", "BE", "ST"})

# Heuristic regex matching SME / PCAS in any common broker encoding.
# Anchored to non-alphanumerics so we never false-positive on substrings
# inside other tokens (e.g. "SMEIPO" vs. "SME IPO").
_SME_TOKEN_RE = re.compile(r"(?:^|[^A-Z0-9])SME(?:$|[^A-Z0-9])")
_PCAS_TOKEN_RE = re.compile(r"(?:^|[^A-Z0-9])(?:PCAS|PERIODIC\s*CALL\s*AUCTION)(?:$|[^A-Z0-9])")
