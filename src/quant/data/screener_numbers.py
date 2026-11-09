"""Turn screener.in display strings into comparable numbers. Pure Python, no Blue import.

Flow stage: Screener rules (stage 6, sub-stage 2a)
Status: PHASE 1 IMPLEMENTED 2026-11-06 (ported from Blue_engine/screener/client/numbers.py)
Parity: tests/parity/test_screener_numbers_parity.py (loads Blue's numbers.py alone; stdlib only).

WHY THIS FILE EXISTS
--------------------
screener.in returns human-formatted strings, not numbers:
    "1,23,456 Cr."   Indian lakh/crore grouping, currency symbol, unit suffix
    "18.5"           plain ratio
    "-"              NOT ZERO. "not applicable / not reported"
    ""               NOT ZERO. parse failed or tab absent
    "12.3%"          percent sign

RULE: A missing value is never 0. It is None, and it FAILS every threshold.
A company that has not reported its debt must not show debt/equity = 0.0 and pass a "< 1" filter.

TIME UNITS ARE EXPLICIT. Nothing here infers whether a cell is a day, a year or a quarter.
Growth in ``quarters`` is QoQ; in ``profit-loss`` it is YoY. The callers say which they read.

NOTE ON unit_scale: the docstring of ``to_number`` lists "absolute" as the rupee option.
The code checks the string "absolute" (not "absolute_cr"). The port keeps the code's behaviour.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Guard sets
# --------------------------------------------------------------------------- #

#: Strings that mean "no value". Compared case-insensitively after normalising.
_NULL_TOKENS = {
    "", "-", "--", "—", "–", "n/a", "na", "null", "none", "nil",
    "not available", "not applicable", "–", "—",
}

#: Magnitude suffixes -> multiplier in the cell's OWN unit (crores).
_SUFFIX_MULTIPLIER: Dict[str, float] = {
    "cr": 1.0,
    "crore": 1.0,
    "crores": 1.0,
    "lakh": 0.01,
    "lakhs": 0.01,
    "lac": 0.01,
    "mn": 0.1,
    "million": 0.1,
    "bn": 100.0,
    "billion": 100.0,
    "k": 0.00001,
    "thousand": 0.00001,
}

#: 1 crore in absolute rupees.
CRORE = 1e7

_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*|[-+]?\.\d+")
_PCT_RE = re.compile(r"%")


# --------------------------------------------------------------------------- #
# Core conversion
# --------------------------------------------------------------------------- #
def clean(text: Any) -> str:
    """Normalise a raw cell to a bare string for inspection (logging, debugging)."""
    if text is None:
        return ""
    return " ".join(str(text).replace("\xa0", " ").split()).strip()


def is_null_token(text: Any) -> bool:
    """True when the cell explicitly says "no value" (``-``, ``N/A``, ...)."""
    return clean(text).lower().rstrip(".:") in _NULL_TOKENS


def to_number(value: Any, unit_scale: Optional[str] = None) -> Optional[float]:
    """Parse one screener.in cell to a float, or ``None`` if it has no value.

    Args:
        value: the raw cell (str, int, float, None).
        unit_scale:
            * ``None``       - the number in the cell's own unit. "1,23,456 Cr." -> 123456.0 (crores).
            * ``"cr"``       - forced into crores ("50 Lakh" -> 0.5).
            * ``"absolute"`` - a crore cell times ``CRORE`` (rupees). A bare number with no suffix -> None.

    Design rule: NEVER returns 0 for a missing or blank cell.
    """
    if value is None:
        return None
    # bool is an int subclass; a True in a numeric field is a bug, not a 1.
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = clean(value)
    if is_null_token(text):
        return None

    lowered = text.lower()

    # Percent sign is decorative here; the sign of a change is in the digits.
    lowered = lowered.replace("%", " ")
    # Currency symbols carry no magnitude.
    lowered = lowered.replace("₹", " ").replace("$", " ")

    num_match = _NUM_RE.search(lowered)
    if num_match is None:
        return None
    try:
        raw = float(num_match.group(0).replace(",", ""))
    except ValueError:
        return None

    # Magnitude suffix AFTER the number (e.g. "1,23,456 cr.").
    # `suffix_found` is kept separate from the multiplier: "cr" has multiplier 1.0,
    # so `mult != 1.0` would wrongly treat "1 Cr." as a bare number.
    suffix_mult = 1.0
    suffix_found = False
    tail = lowered[num_match.end():]
    for token in re.findall(r"[a-z]+", tail):
        if token in _SUFFIX_MULTIPLIER:
            suffix_mult = _SUFFIX_MULTIPLIER[token]
            suffix_found = True
            break

    if suffix_found:
        in_crores = raw * suffix_mult
        if unit_scale == "absolute":
            return in_crores * CRORE
        return in_crores

    # No suffix on the cell.
    if unit_scale == "absolute":
        # A bare number cannot be assumed to be crores. Refuse rather than invent a magnitude.
        return None
    return raw


def to_percent(value: Any) -> Optional[float]:
    """Parse a growth/return cell to a percentage number. Same as ``to_number``; sign preserved."""
    return to_number(value)


# --------------------------------------------------------------------------- #
# Ratio lookup (name -> numeric field, with stable aliases)
# --------------------------------------------------------------------------- #
#: Canonical metric -> (id aliases, name fragments). Ids are the stable key.
RATIO_ALIASES: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "market_cap":     (("market-cap", "mar-cap"), ("market cap", "mkt cap")),
    "current_price":  (("current-price",), ("current price",)),
    "high_low":       (("high-low", "highlow"), ("high / low", "high/low")),
    "pe":             (("stock-pe", "pe"), ("stock p/e", "p/e", "price to earning")),
    "book_value":     (("book-value",), ("book value",)),
    "dividend_yield": (("dividend-yield",), ("dividend yield",)),
    "roce":           (("roce",), ("roce", "return on capital")),
    "roe":            (("roe",), ("roe", "return on equity")),
    "face_value":     (("face-value",), ("face value",)),
    "eps":            (("eps", "stock-eps"), ("eps", "earnings per share")),
    "debt":           (("debt", "total-debt"), ("total debt", "borrowings")),
    "debt_equity":    (("debt-equity",), ("debt to equity", "debt/equity", "debt to eq")),
    "sales":          (("sales",), ("sales",)),
    "net_profit":     (("net-profit",), ("net profit",)),
    "promoter_holding": (("promoter-holding",), ("promoter holding",)),
}


def _normalise_name(name: str) -> str:
    return clean(name).lower()


def ratio_lookup(top_ratios: Iterable[Dict[str, Any]], metric: str) -> Optional[str]:
    """Find a top-ratio cell by canonical metric name: id first, then name text.

    Returns the RAW string value (the caller picks the unit with ``to_number``), or None.
    """
    aliases = RATIO_ALIASES.get(metric)
    if not aliases:
        return None
    id_aliases, name_fragments = aliases

    rows = list(top_ratios or [])

    for row in rows:
        rid = _normalise_name(str(row.get("id") or ""))
        if rid and rid in id_aliases:
            value = row.get("value")
            if not is_null_token(value):
                return value

    for row in rows:
        name = _normalise_name(str(row.get("name") or ""))
        if not name:
            continue
        for frag in name_fragments:
            if frag in name:
                value = row.get("value")
                if not is_null_token(value):
                    return value
    return None


def metric_number(top_ratios: Iterable[Dict[str, Any]], metric: str,
                  unit_scale: Optional[str] = None) -> Optional[float]:
    """``ratio_lookup`` + ``to_number`` in one call."""
    return to_number(ratio_lookup(top_ratios, metric), unit_scale=unit_scale)


# --------------------------------------------------------------------------- #
# Table helpers (used by the fundamental fallback path)
# --------------------------------------------------------------------------- #
def table_row_lookup(table: Optional[Dict[str, Any]],
                     label_fragments: Iterable[str]) -> Optional[Dict[str, str]]:
    """Find the row of a financial table whose FIRST column matches a label (substring, case-insensitive)."""
    if not table or not isinstance(table, dict):
        return None
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    if not headers:
        return None
    label_col = headers[0]
    frags = [_normalise_name(f) for f in label_fragments]
    for row in rows:
        label = _normalise_name(str(row.get(label_col, "")))
        if not label:
            continue
        for frag in frags:
            if frag in label:
                return row
    return None


def latest_period_value(row: Optional[Dict[str, Any]],
                        headers: Optional[List[str]] = None,
                        unit_scale: Optional[str] = None) -> Optional[float]:
    """The LAST numeric cell of a table row (most recent period). Scans from the right."""
    if not row:
        return None
    keys = list(headers) if headers else list(row.keys())
    # Skip the label column (index 0) when we know the headers.
    if headers and len(keys) > 1:
        keys = keys[1:]
    else:
        keys = keys[1:] if len(keys) > 1 else keys
    for key in reversed(keys):
        if key not in row:
            continue
        num = to_number(row.get(key), unit_scale=unit_scale)
        if num is not None:
            return num
    return None


__all__ = [
    "CRORE",
    "RATIO_ALIASES",
    "clean",
    "is_null_token",
    "to_number",
    "to_percent",
    "ratio_lookup",
    "metric_number",
    "table_row_lookup",
    "latest_period_value",
]
