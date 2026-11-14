"""
numbers.py - turn screener.in's display strings into comparable numbers.

WHY THIS FILE EXISTS (AND WHY IT IS THE MOST IMPORTANT ONE HERE)
---------------------------------------------------------------
screener.in returns human-formatted strings, not numbers:

    "₹1,23,456 Cr."     Indian lakh/crore grouping, currency symbol, unit suffix
    "18.5"              plain ratio
    "-"                 NOT ZERO. "not applicable / not reported"
    ""                  NOT ZERO. parse failed or tab absent
    "1,234.56"          thousands-grouped, no suffix
    "12.3%"             percent sign
    "₹1,234 / ₹1,100"   a High/Low cell: two numbers in one string
    "Debt to equity"    a *label*, if we ever grab the wrong cell

The single most common way a fundamental screener lies to you is treating "-"
or "" as 0.0. A company that has NOT REPORTED its debt would then show
debt/equity = 0.0 and pass a "< 1" filter — exactly the wrong direction, on
exactly the companies you most want excluded. So:

    ** A missing value is never 0. It is None, and it FAILS every threshold. **

Indian digit grouping matters too: "1,00,000" is 100000, and naive
``float(s.replace(",", ""))`` happens to handle that correctly — but
"₹1.2 Cr." is 12,000,000 in absolute rupees, and the ``Cr``/``Lakh`` suffix is
everything. ``to_number(value, unit_scale=...)`` exists so the caller decides
whether it wants crores or rupees, and never guesses.

TIME UNITS ARE EXPLICIT
-----------------------
Nothing here infers whether a cell is a day, a year, or a quarter. Growth
percentages in ``quarters`` are QoQ, in ``profit-loss`` they are YoY. The
individual parsers say which they read; this module just converts.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Guard sets
# --------------------------------------------------------------------------- #

#: Strings that mean "no value". Compared case-insensitively after normalising.
#: Anything in here MUST become ``None``, never 0.
_NULL_TOKENS = {
    "", "-", "--", "—", "–", "n/a", "na", "null", "none", "nil",
    "not available", "not applicable", "\u2013", "\u2014",
}

#: Magnitude suffixes seen in screener.in ratio cells -> multiplier in the
#: cell's OWN unit. "Cr." = 1 crore = 10^7. Kept as a separate map because a
#: caller asking for rupees wants ``* 1e7`` while a caller asking for crores
#: wants ``* 1``.
_SUFFIX_MULTIPLIER: Dict[str, float] = {
    "cr": 1.0,          # crore (10^7 in absolute terms; see below)
    "crore": 1.0,
    "crores": 1.0,
    "lakh": 0.01,       # 1 lakh = 0.01 crore
    "lakhs": 0.01,
    "lac": 0.01,
    "mn": 0.1,          # 1 million = 0.1 crore
    "million": 0.1,
    "bn": 100.0,        # 1 billion = 100 crore
    "billion": 100.0,
    "k": 0.00001,       # 1 thousand = 1e-5 crore
    "thousand": 0.00001,
}

#: 1 crore in absolute rupees. Used only when a caller explicitly asks for
#: absolute currency (``to_number(..., unit_scale="absolute_cr")``).
CRORE = 1e7

_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*|[-+]?\.\d+")
_PCT_RE = re.compile(r"%")


# --------------------------------------------------------------------------- #
# Core conversion
# --------------------------------------------------------------------------- #
def clean(text: Any) -> str:
    """Normalise a raw cell to a bare string for inspection.

    Not a number converter - this is what you call when you want to *look* at a
    value (logging, debugging, a config override). ``to_number`` is what you
    call when you want to *compare* one.
    """
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
        unit_scale: how to treat a magnitude suffix on the cell.
            * ``None``      - return the number in the cell's own unit. A cell
                              "1,23,456 Cr." -> 123456.0 (i.e. crores).
            * ``"cr"``      - force the result into crores (same as None for a
                              Cr cell, but converts "50 Lakh" -> 0.5).
            * ``"absolute"``- multiply a crore cell by ``CRORE`` so the result
                              is in rupees.

    Returns:
        ``float`` when the cell holds a number, else ``None``.

    Design rule: this function NEVER returns 0 for a missing/blank cell. Every
    caller's threshold check is a ``>=``/``<=`` comparison, and ``None`` fails
    those comparisons loudly instead of passing quietly. That is the safety
    property the whole screener depends on.
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

    # Percent is purely decorative here: the caller asked for a number and the
    # sign of the change is not encoded in "%", it is encoded in the digits.
    lowered = lowered.replace("%", " ")
    # Currency symbols (rupee, dollar) and stray separators carry no magnitude.
    lowered = lowered.replace("₹", " ").replace("$", " ")

    num_match = _NUM_RE.search(lowered)
    if num_match is None:
        return None
    try:
        raw = float(num_match.group(0).replace(",", ""))
    except ValueError:
        return None

    # Detect a magnitude suffix AFTER the number (e.g. "1,23,456 cr.").
    #
    # IMPORTANT: `suffix_found` is tracked SEPARATELY from the multiplier.
    # "cr" maps to a multiplier of 1.0 (a crore IS the base unit), so using
    # `mult != 1.0` as the "did we see a suffix?" test would treat "1 Cr." as a
    # bare number and refuse to resolve it to absolute rupees. That is a silent
    # magnitude bug, so the two concepts are kept distinct on purpose.
    suffix_mult = 1.0
    suffix_found = False
    tail = lowered[num_match.end():]
    for token in re.findall(r"[a-z]+", tail):
        if token in _SUFFIX_MULTIPLIER:
            suffix_mult = _SUFFIX_MULTIPLIER[token]
            suffix_found = True
            break

    if suffix_found:
        # Normalise to crores first, then apply the caller's unit request.
        in_crores = raw * suffix_mult
        if unit_scale == "absolute":
            return in_crores * CRORE
        return in_crores

    # No suffix on the cell.
    if unit_scale == "absolute":
        # A bare number with no suffix cannot be safely assumed to be crores.
        # Refuse rather than invent a magnitude - this is the difference between
        # "we don't know" and a wrong market-cap filter.
        return None
    return raw


def to_percent(value: Any) -> Optional[float]:
    """Parse a growth/return cell to a percentage number.

    Identical to :func:`to_number` but kept as a named entry point so call sites
    document that the unit is percent. A cell that already carries "%" parses
    the same way; the sign is preserved ("-35.09" -> -35.09).
    """
    return to_number(value)


# --------------------------------------------------------------------------- #
# Ratio lookup (name -> numeric field, with stable aliases)
# --------------------------------------------------------------------------- #
#: Canonical metric -> the ``id`` attribute aliases screener.in uses on the
#: ``ul#top-ratios`` items, plus name fragments to match when the id is absent.
#: Ids are the stable key; names are the fallback and are matched
#: case-insensitively on a whitespace-collapsed string.
RATIO_ALIASES: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    # canonical        (id aliases)                          (name fragments)
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


def ratio_lookup(top_ratios: Iterable[Dict[str, Any]],
                 metric: str) -> Optional[str]:
    """Find a top-ratio cell by canonical metric name, id first, then name text.

    Returns the *raw string* value (so the caller chooses its unit via
    :func:`to_number`), or ``None``.

    Matching order is deliberate:
      1. ``id`` attribute against the canonical id aliases (stable).
      2. Name text against the name fragments (drifts: "Stock P/E" vs "P/E").
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
    """``ratio_lookup`` + ``to_number`` in one call. Convenience, not new logic."""
    return to_number(ratio_lookup(top_ratios, metric), unit_scale=unit_scale)


# --------------------------------------------------------------------------- #
# Table helpers (used by the fundamental fallback path)
# --------------------------------------------------------------------------- #
def table_row_lookup(table: Optional[Dict[str, Any]],
                     label_fragments: Iterable[str]) -> Optional[Dict[str, str]]:
    """Find the row of a financial table whose FIRST column matches a label.

    screener.in financial tables put the human label in the first column
    ("Debt to equity", "Borrowings", "Net Profit") and the periods across.
    We match case-insensitively on a substring so "Borrowings" and "Total
    Borrowings" both hit.

    Returns the row dict (keyed by the table's own headers) or ``None``.
    """
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
    """Take the LAST numeric cell of a table row (the most recent period).

    screener.in orders periods oldest -> newest, so "latest" is the rightmost
    numeric cell. We scan from the right and take the first cell that parses,
    which skips trailing empty/annotated columns.
    """
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
