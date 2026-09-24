"""
candlestick.py - unicode-block ASCII candle sparkline + per-candle renderer.

We use 8 vertical levels (U+2581 .. U+2588) so a 3-candle sparkline
fits in ~6 terminal rows. Pure output, no live updates - the
dashboard layer owns the refresh.
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Sequence

# Vertical block elements, low to high. Eight levels give enough
# resolution for a 1-char-wide sparkline while staying readable.
_BLOCKS = ["\u2581", "\u2582", "\u2583", "\u2584", "\u2585", "\u2586", "\u2587", "\u2588"]


def _ohlc(candle: Mapping[str, float]) -> tuple:
    """Return (open, high, low, close) as floats. Tolerant of missing keys."""
    try:
        o = float(candle.get("open", 0))
        h = float(candle.get("high", 0))
        l = float(candle.get("low", 0))
        c = float(candle.get("close", 0))
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return o, h, l, c


def render_candles(
    candles: Sequence[Mapping[str, float]],
    *,
    width: int = 6,
    height: int = 4,
) -> str:
    """Render ``candles`` as a small ASCII block chart.

    Args:
        candles: list of {open, high, low, close} dicts (oldest -> newest).
        width: number of characters per candle (1-3 recommended).
        height: total height of the chart in rows.

    Returns:
        A multi-line string with the chart. Empty list -> empty string.
    """
    if not candles:
        return ""
    parsed: List[tuple] = [_ohlc(c) for c in candles]
    highs = [p[1] for p in parsed]
    lows = [p[3] for p in parsed]
    hi = max(highs)
    lo = min(lows)
    span = hi - lo
    if span <= 0:
        return ""

    # Each candle gets ``width`` columns. We map the price axis to
    # [0, height-1] and then bucket each column into the right block
    # glyph.
    rows: List[str] = []
    for row in range(height - 1, -1, -1):
        line = []
        for o, h, l, c in parsed:
            if width == 1:
                col = _glyph_for_row(o, h, l, c, row, height, hi, lo, span)
                line.append(col)
            else:
                # Multi-char candle: the body is open..close; the wick
                # extends to high/low.
                cells = _multi_char_candle(o, h, l, c, row, height, hi, lo, span, width)
                line.append(cells)
        rows.append("".join(line).rstrip())
    return "\n".join(rows)


def _glyph_for_row(o, h, l, c, row, height, hi, lo, span):
    """Return a single character for one candle at one row."""
    if span == 0:
        return " "
    # Map the row to a price level.
    row_low = lo + (span * row / height)
    row_high = lo + (span * (row + 1) / height)
    # Body range:
    body_low, body_high = (min(o, c), max(o, c))

    if row_high <= body_low or row_low >= body_high:
        # This row is entirely above or below the body. Could still
        # be a wick though.
        if l <= row_low < h:
            return _BLOCKS[3]  # a thin mid-glyph for the wick
        return " "
    # We're inside the body. The full block at the top of the body
    # gives a nice "filled" look.
    return _BLOCKS[7]


def _multi_char_candle(o, h, l, c, row, height, hi, lo, span, width):
    """Render a candle of ``width`` characters at ``row``."""
    if span == 0:
        return " " * width
    body_low, body_high = (min(o, c), max(o, c))
    row_low = lo + (span * row / height)
    row_high = lo + (span * (row + 1) / height)
    chars = []
    for col in range(width):
        # Approximate the body as the middle 60% of the candle,
        # wicks as the outer edges.
        if width == 1:
            inside_body = True
        else:
            edge = max(1, width // 5)
            inside_body = (col >= edge) and (col < width - edge)
        if inside_body:
            if row_high > body_low and row_low < body_high:
                chars.append(_BLOCKS[7])
            elif l <= row_low < h or l <= row_high <= h:
                chars.append(_BLOCKS[3])
            else:
                chars.append(" ")
        else:
            # wick zone
            if l <= row_low < h or l <= row_high <= h or (row_low >= l and row_high <= h):
                chars.append(_BLOCKS[2])
            else:
                chars.append(" ")
    return "".join(chars)


def candle_sparkline(
    candles: Sequence[Mapping[str, float]],
    *,
    width: int = 3,
    height: int = 3,
) -> str:
    """Compact form of :func:`render_candles` for inline display.

    Same semantics, but defaults are tighter so the result fits in
    a single row of the dashboard table.
    """
    return render_candles(candles, width=width, height=height)