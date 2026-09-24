"""
lot.py - Exchange lot-size helpers (Feature 7 - Mandatory Lot-Size Rounding).

Responsibility
--------------
Two functions only:

  * :func:`lot_quantity` - pure integer math. Floors a desired
    child quantity to the nearest whole lot multiple (e.g. a slice
    of 2,500 against a 1,200-share lot becomes 2,400). Used by the
    POV slicer AFTER its normal cap-by-remaining/max-slice logic so
    the broker never sees a quantity it will reject.

  * :func:`resolve_lot_size` - a small SDK-aware adapter that looks
    up the official lot size from the symbol master. If the master
    is unavailable we return ``None`` so the caller can fall back to
    the legacy ``max_slice_quantity`` ceiling (no surprise orders).

Why a separate module
---------------------
Lot rounding is a regulatory concern, not a slicing one. Keeping it
in its own file means a TWAP / Iceberg / VWAP slicer can reuse the
same helper without copying the math, and unit tests can pin the
rounding behaviour without a full SDK fixture.

Design rules
------------
* No exceptions for absent data: every helper returns a value the
  caller can check. The slicer is the one that decides to skip.
* No float arithmetic in the rounding path - integer math only so
  no rounding drift accumulates over thousands of child orders.
* JSON-overridable: a caller that already knows the lot size can
  pass it directly and skip the SDK lookup entirely.
"""
from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# Pure integer lot rounding
# ---------------------------------------------------------------------------
def lot_quantity(quantity: int, lot_size: int) -> int:
    """Return the largest valid exchange-lot multiple <= ``quantity``.

    This is the canonical implementation of the "round down to lot
    size" rule (Feature 7). It is intentionally written without any
    floating-point arithmetic so the result is always exact.

    Args:
        quantity:  The unconstrained child quantity (shares/contracts).
        lot_size:  The exchange-mandated lot size. ``<=0`` disables
                   rounding and the input is returned unchanged.

    Returns:
        The floored multiple of ``lot_size`` that is <= ``quantity``.
        ``0`` when ``quantity`` or ``lot_size`` is non-positive.

    Examples:
        >>> lot_quantity(2500, 1200)
        2400
        >>> lot_quantity(1200, 1200)
        1200
        >>> lot_quantity(1199, 1200)
        0
        >>> lot_quantity(1500, 0)
        1500
    """
    if quantity <= 0 or lot_size <= 0:
        return max(0, quantity) if lot_size <= 0 else 0
    return (quantity // lot_size) * lot_size



def _resolve_lot_size_from_master_db(
    symbol: str,
    exchange: str,
) -> Optional[int]:
    """Read the official lot size from the platform's master contract DB.

    This is the **primary** automatic path and the same one the
    broker-facing :mod:`services.volume_participation_service` uses.
    It works for every broker whose master contract download
    populates the ``symtoken.lotsize`` column - m.Stock, Zerodha,
    Angel, dhan, etc. all do.

    Returns ``None`` (NOT an exception) when the symbol is missing,
    the DB is unavailable, or the lot size is zero/None. The
    caller treats ``None`` as "skip rounding".
    """
    if not symbol or not exchange:
        return None
    try:
        from database.token_db_enhanced import get_symbol_info
    except Exception:
        # Platform DB not importable (running pov_engine outside
        # OpenAlgo). Return None so the helper degrades safely.
        return None
    try:
        info = get_symbol_info(symbol, exchange)
    except Exception:
        return None
    if info is None:
        return None
    raw = getattr(info, "lotsize", None)
    try:
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _resolve_lot_size_from_sdk(
    client: Any,
    *,
    symbol: str,
    exchange: str,
) -> Optional[int]:
    """Fallback SDK lookup for adapters that expose ``client.symbol()``.

    The current OpenAlgo SDK (v2.0.3) and the m.Stock broker adapter
    do NOT implement a ``symbol()`` method, so for them this path
    is always a no-op. The fallback exists so the framework keeps
    working with:

      * Future OpenAlgo SDK releases that may add ``client.symbol``.
      * Custom broker adapters built outside the OpenAlgo platform.
      * The legacy ``getsymbol`` / ``instrument`` aliases for
        back-compat with very old SDK versions.

    Returns ``None`` when no SDK method is present or no lot size
    is found in the response.
    """
    if client is None or not symbol or not exchange:
        return None

    for method_name in ("symbol", "getsymbol", "instrument"):
        method = getattr(client, method_name, None)
        if method is None:
            continue
        response: Any = None
        try:
            response = method(symbol=symbol, exchange=exchange)
        except TypeError:
            try:
                response = method(symbol, exchange)
            except Exception:
                response = None
        except Exception:
            response = None
        if not isinstance(response, dict) or response.get("status") != "success":
            return None
        data = response.get("data") or {}
        for key in ("lotsize", "lot_size", "trading_lot_size", "lotSize"):
            raw = data.get(key)
            if raw is None:
                continue
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None
    return None


# ---------------------------------------------------------------------------
# SDK-aware lot resolution
# ---------------------------------------------------------------------------
def resolve_lot_size(
    client: Any,
    *,
    symbol: str,
    exchange: str,
    override: Any = None,
) -> Optional[int]:
    """Resolve the official lot size for ``symbol`` on ``exchange``.

    Resolution order (highest priority first):

      1. ``override`` - an explicit integer/lot size passed by the
         caller. The JSON config can supply this when the user
         already knows the lot size and wants to skip every
         auto-lookup. The override ALWAYS wins.
      2. The platform's master contract DB via
         :func:`_resolve_lot_size_from_master_db`. This is the
         automatic path that works for m.Stock, Zerodha, Angel,
         etc. - every broker whose master-contract download
         populates the ``symtoken.lotsize`` column.
      3. The OpenAlgo SDK fallback via
         :func:`_resolve_lot_size_from_sdk`. Used only when the
         master DB is unavailable (e.g. running pov_engine outside
         the OpenAlgo platform) AND the SDK has a ``symbol()``
         method. For m.Stock today this is a no-op.
      4. ``None`` when no source has the data. The slicer logs
         ``"no lot size resolved; rounding disabled"`` and proceeds
         with the slice quantity unchanged.

    Args:
        client:   An initialized OpenAlgo SDK client. Used only by
                  path #3.
        symbol:   The trading symbol, e.g. ``"INFY"``.
        exchange: The exchange code, e.g. ``"NSE"``.
        override: Optional explicit lot size from the JSON config.

    Returns:
        A positive integer lot size, or ``None`` when the lot size
        cannot be determined. Returns ``None`` (never ``0``) for
        invalid input so the caller's truthiness check is simple.
    """
    # Path 1: explicit override wins.
    if override is not None:
        try:
            value = int(override)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    if not symbol or not exchange:
        return None

    # Path 2: master contract DB - works for every broker whose
    # master download populates symtoken.lotsize (m.Stock, etc.).
    master_value = _resolve_lot_size_from_master_db(symbol, exchange)
    if master_value is not None:
        return master_value

    # Path 3: SDK fallback (no-op for m.Stock today; reserved for
    # future SDK releases and custom broker adapters).
    sdk_value = _resolve_lot_size_from_sdk(
        client, symbol=symbol, exchange=exchange
    )
    if sdk_value is not None:
        return sdk_value

    # Path 4: nothing worked - caller will skip lot rounding.
    return None


__all__ = [
    "lot_quantity",
    "resolve_lot_size",
    "_resolve_lot_size_from_master_db",
    "_resolve_lot_size_from_sdk",
]
