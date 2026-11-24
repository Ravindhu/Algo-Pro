"""
_pattern.py - shared OCL candlestick-pattern helpers.

Used by both:
  - signals/ocl_keylevel.py (single-symbol signal that fires a trade)
  - screener/ocl_screener.py   (multi-symbol scanner that reports matches)

Extracted so that the validation and trigger-level logic are guaranteed
to stay in lock-step. The OCL (Open-Close-Low / Open-Close-High) family
of 3-candle patterns is:
  - RRG  (Red-Red-Green):   two bearish candles then a bullish candle -> bullish reversal
  - GRR  (Green-Red-Red):   bullish then two bearish candles            -> bearish reversal

The trigger level is the body low (RRG) or body high (GRR) of the
second and third candles, whichever is more extreme. It is the price
the LTF must touch to confirm the pattern.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

# Pattern identifiers (kept as constants so callers and tests don't pass raw strings)
PATTERN_RRG = "RRG"  # Red-Red-Green  -> bullish reversal -> BUY
PATTERN_GRR = "GRR"  # Green-Red-Red  -> bearish reversal -> SELL

VALID_PATTERNS = (PATTERN_RRG, PATTERN_GRR)


def extract_ohlc(candle: Any) -> Dict[str, float]:
    """Return a normalized {open, high, low, close} dict from a candle.

    Accepts a pandas Series, a dict, or any object with a ``to_dict()``
    method. Missing or non-numeric values coerce to 0.0 so downstream
    math doesn't blow up on a partial response. Anything that is
    neither a mapping nor a ``to_dict()``-bearing object (e.g. a bare
    string) returns a zeros dict.
    """
    try:
        if hasattr(candle, "to_dict"):
            candle = candle.to_dict()
        if not isinstance(candle, Mapping):
            return {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}
        return {
            "open": float(candle.get("open", 0)),
            "high": float(candle.get("high", 0)),
            "low": float(candle.get("low", 0)),
            "close": float(candle.get("close", 0)),
        }
    except (TypeError, ValueError, AttributeError):
        return {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}


def validate_pattern(
    c1: Mapping[str, float],
    c2: Mapping[str, float],
    c3: Mapping[str, float],
    pattern_type: str,
    min_body_pct: float = 0.3,
) -> bool:
    """Return True if candles c1, c2, c3 form the requested pattern.

    Args:
        c1, c2, c3: OHLC dicts for candles 1..3 (oldest to newest).
        pattern_type: "RRG" or "GRR".
        min_body_pct: RETAINED FOR SIGNATURE COMPATIBILITY ONLY. The
            body-size filter was removed by operator instruction, so this
            value is accepted and ignored. Pattern validation is now a pure
            candlestick-shape test.
    """
    pattern_type = (pattern_type or "").upper()
    if pattern_type not in VALID_PATTERNS:
        return False

    c1_bearish = c1["close"] < c1["open"]
    c1_bullish = c1["close"] > c1["open"]
    c2_bearish = c2["close"] < c2["open"]
    c3_bearish = c3["close"] < c3["open"]
    c3_bullish = c3["close"] > c3["open"]

    if pattern_type == PATTERN_RRG:
        pattern_match = c1_bearish and c2_bearish and c3_bullish
    else:  # PATTERN_GRR
        pattern_match = c1_bullish and c2_bearish and c3_bearish

    if not pattern_match:
        return False

    # NOTE: the body-size filter was REMOVED here (operator instruction).
    # The old rule rejected any candle whose
    #     |close - open| / (high - low)  <  min_body_pct
    # which discarded ~60% of shape-matching patterns on real INFY daily bars
    # (113 raw shapes -> 45 with the 0.3 default). Pattern validation is now a
    # pure candlestick-shape test; candle quality is no longer part of it.
    #
    # min_body_pct remains in the signature so existing callers and JSON
    # configs keep working unchanged - it is simply no longer consulted.
    _ = min_body_pct

    return True


def calculate_trigger_level(
    c2: Mapping[str, float],
    c3: Mapping[str, float],
    pattern_type: str,
) -> Optional[float]:
    """Return the price the LTF must touch to confirm the pattern.

    - RRG (bullish): min(body_low(c2), body_low(c3))  -- the demand level
    - GRR (bearish): max(body_high(c2), body_high(c3)) -- the supply level

    Returns None if the pattern is unknown.
    """
    pattern_type = (pattern_type or "").upper()
    if pattern_type not in VALID_PATTERNS:
        return None

    c2_body_low = min(c2["open"], c2["close"])
    c2_body_high = max(c2["open"], c2["close"])
    c3_body_low = min(c3["open"], c3["close"])
    c3_body_high = max(c3["open"], c3["close"])

    if pattern_type == PATTERN_RRG:
        return min(c2_body_low, c3_body_low)
    return max(c2_body_high, c3_body_high)


def action_for_pattern(pattern_type: str) -> str:
    """Map pattern to trade direction. RRG -> BUY, GRR -> SELL."""
    pattern_type = (pattern_type or "").upper()
    if pattern_type == PATTERN_RRG:
        return "BUY"
    if pattern_type == PATTERN_GRR:
        return "SELL"
    return "BUY"
