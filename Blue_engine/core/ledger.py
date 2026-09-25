"""ledger.py - Persistent child-order ledger for POV runs (Feature 9).

Extended for Feature 13 to also persist:

  * the running session VWAP (updated per child fill),
  * the rolling slippage sample window,
  * the arrival-price tick window (one per open child).

All new fields degrade gracefully when the DB is unavailable: the
in-memory stand-in carries the same state, and the platform DB
helpers are called defensively (try/except + log).
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Optional


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ARCHITECTURE (locked): Python (blue_engine) -> OpenAlgo (server) -> Broker
#
# The engine must NOT import from the OpenAlgo tree. The previous code did
#     from database import pov_db as _pov_db
# which reached into the platform's internals - exactly the coupling this
# architecture exists to remove. It was also failing silently in production
# (see log/pov.out.log: "create_execution() got an unexpected keyword
# argument 'strategy'"), so the mirror was never actually serving state.
#
# The in-memory ledger below is the AUTHORITATIVE implementation; the DB
# helpers in database.pov_db were only ever an optional mirror. The platform
# side of that mirror is retired (archived under archive/server_pov_nse/).
#
# If durable cross-restart state is wanted later, implement it INSIDE this
# package (e.g. blue_engine/core/ledger_store.py on plain sqlite3) rather than
# re-importing anything from openalgo/.
# ---------------------------------------------------------------------------
_pov_db = None
_LEDGER_AVAILABLE = False

# Default sizes for the in-memory rolling windows. They are overridable
# per-call by the slicer; the defaults match the design doc.
_DEFAULT_SLIPPAGE_WINDOW = 10
_DEFAULT_ARRIVAL_TICKS = 20


@dataclass
class _InMemoryExecution:
    """Tiny in-memory stand-in for the DB row."""

    id: str
    request_data: dict
    phase: str = "accumulating"
    order_count: int = 0
    cancel_count: int = 0
    filled_quantity: int = 0
    exit_filled_quantity: int = 0
    exit_target_quantity: int = 0
    exit_submitted_quantity: int = 0
    reserved_quantity: int = 0
    submitted_quantity: int = 0
    last_order_at: Optional[datetime] = None
    last_cancel_at: Optional[datetime] = None
    children: list = field(default_factory=list)

    # ---- Feature 13 extensions -------------------------------------- #
    # Session VWAP numerator/denominator. session_vwap = num / den.
    _vwap_num: float = 0.0
    _vwap_den: float = 0.0
    # Rolling slippage samples (basis points).
    slippage_samples: Deque[float] = field(default_factory=deque)
    # Arrival-price window for the currently-open child (if any).
    arrival_window: "Any" = None  # ArrivalPriceWindow (lazy import)


_IN_MEMORY_EXECUTIONS = {}

#: Flipped to True the first time the platform DB path raises. Once a DB
#: helper has failed we stop trying it for the life of the process so every
#: ledger call falls back to the in-memory stand-in. Without this, a DB that
#: is importable but broken (wrong schema, missing table) makes the DB path
#: swallow the error and skip the in-memory mirror entirely, so fills are
#: silently lost.
_DB_DEGRADED = False


def _use_db() -> bool:
    """True only when the DB mirror is importable AND not yet degraded."""
    return _LEDGER_AVAILABLE and _pov_db is not None and not _DB_DEGRADED


def _degrade_db(exc) -> None:
    """Disable the DB mirror for the rest of the process after a failure."""
    global _DB_DEGRADED
    if not _DB_DEGRADED:
        _DB_DEGRADED = True
        logger.warning(
            "blue_engine.ledger: degrading to in-memory only after DB error: %s",
            exc,
        )


def _in_memory_get(execution_id):
    row = _IN_MEMORY_EXECUTIONS.get(execution_id)
    if row is None:
        raise KeyError(execution_id)
    return row


def _phase_label(child):
    return str(child.get("phase", "accumulating") or "accumulating")


def make_execution_id(strategy_label):
    label = (strategy_label or "POV").strip().replace(" ", "_")
    return f"{label}-{uuid.uuid4().hex[:12]}"


def api_key_hash(api_key):
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def create_execution(*, execution_id, api_key=None, request_data=None, **extra):
    """Create an execution row.

    ``request_data`` is the preferred, self-describing payload. For
    backwards compatibility, callers may instead pass the individual
    fields (strategy / symbol / exchange / action / total_quantity / mode /
    phase) as keyword arguments; they are folded into ``request_data`` so
    the in-memory stand-in still carries them.
    """
    if request_data is None:
        request_data = dict(extra) if extra else {}
    elif extra:
        merged = dict(request_data)
        for key, value in extra.items():
            merged.setdefault(key, value)
        request_data = merged
    if _use_db():
        try:
            payload, _created = _pov_db.create_execution(
                execution_id=execution_id,
                api_key_hash=api_key_hash(api_key),
                idempotency_key_hash=hashlib.sha256(
                    execution_id.encode("utf-8")
                ).hexdigest(),
                request_data=request_data,
            )
            return {
                "execution_id": payload.get("execution_id", execution_id),
                "status": payload.get("status", "pending"),
                "filled_quantity": payload.get("filled_quantity", 0),
            }
        except Exception as exc:
            logger.error("blue_engine.ledger.create_execution failed: %s", exc)
            _degrade_db(exc)
    # ---- Feature 13: initialise bounded rolling windows --------------- #
    slippage_window = int(
        (request_data or {}).get("slippage_window_fills", _DEFAULT_SLIPPAGE_WINDOW)
    )
    if slippage_window < 1:
        slippage_window = _DEFAULT_SLIPPAGE_WINDOW
    arrival_ticks = int(
        (request_data or {}).get("arrival_window_ticks", _DEFAULT_ARRIVAL_TICKS)
    )
    if arrival_ticks < 1:
        arrival_ticks = _DEFAULT_ARRIVAL_TICKS
    row = _InMemoryExecution(
        id=execution_id,
        request_data=dict(request_data),
        slippage_samples=deque(maxlen=slippage_window),
        arrival_window=None,  # populated lazily on first child
    )
    _IN_MEMORY_EXECUTIONS[execution_id] = row
    return {"execution_id": execution_id, "status": "pending", "filled_quantity": 0}


def record_child(*, execution_id, order_id=None, quantity=0, ltp=0.0,
                 status="open", avg_price=None, assume_fill=False):
    """Record a placed child order against an execution.

    Thin, forgiving wrapper around the claim/finish lifecycle so callers
    that only have a broker response in hand can persist it in one call.
    Never raises: an unknown execution or a ledger hiccup is logged and
    swallowed, because a bookkeeping miss must not abort a live run.

    ``assume_fill`` (default ``False``) controls whether a broker ACK is
    optimistically counted as a full fill. It must stay ``False`` for live
    trading: an order ACK is not a fill, and crediting the whole quantity
    here made the POV loop believe the parent was complete before any
    shares actually traded. When ``True`` (legacy behaviour, opt-in via the
    slicer's ``assume_fill_on_ack`` config) the child is credited at once;
    the order-update WebSocket re-reports the same quantity and
    :func:`record_fill` is idempotent (it takes the max), so the count is
    never double-added.
    """
    try:
        child_number = claim_child(execution_id, quantity, 0.0, "accumulating")
        if child_number is None:
            return None
        success = str(status).lower() != "rejected"
        finish_child(
            execution_id,
            child_number,
            success=success,
            order_id=order_id,
            message=None if success else str(status),
        )
        if avg_price is None:
            avg_price = ltp
        try:
            _in_memory_get(execution_id).children[-1]["avg_price"] = avg_price
        except Exception:
            pass
        # Only credit the child as filled when the caller explicitly opted in.
        # A real fill arrives later through the order-update WebSocket and is
        # applied by record_fill(), which takes the max of old/new so a
        # duplicate cannot double-count.
        if assume_fill and order_id and success:
            try:
                record_fill(str(order_id), int(quantity), "open")
            except Exception:
                pass
        return child_number
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("blue_engine.ledger.record_child failed: %s", exc)
        return None

def claim_child(execution_id, quantity, observed_volume, phase):
    if _use_db():
        try:
            return _pov_db.claim_child(
                execution_id, int(quantity), float(observed_volume), phase
            )
        except Exception as exc:
            logger.error("blue_engine.ledger.claim_child failed: %s", exc)
            _degrade_db(exc)
            return None
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return None
    child_number = len(row.children) + 1
    row.children.append({
        "child_number": child_number,
        "quantity": int(quantity),
        "observed_volume": float(observed_volume),
        "phase": phase,
        "status": "pending",
    })
    row.reserved_quantity += int(quantity)
    return child_number


def finish_child(execution_id, child_number, *, success, order_id=None, message=None):
    if _use_db():
        try:
            return _pov_db.finish_child(
                execution_id,
                int(child_number),
                success=bool(success),
                order_id=order_id,
                message=message,
            )
        except Exception as exc:
            logger.error("blue_engine.ledger.finish_child failed: %s", exc)
            _degrade_db(exc)
            return None
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return None
    for child in row.children:
        if child.get("child_number") == child_number:
            child["status"] = "open" if success else "rejected"
            if order_id:
                child["orderid"] = str(order_id)
            if message:
                child["message"] = str(message)
            row.reserved_quantity = max(
                0, row.reserved_quantity - int(child.get("quantity", 0))
            )
            if success:
                if _phase_label(child) == "exiting":
                    row.exit_submitted_quantity += int(child.get("quantity", 0))
                else:
                    row.submitted_quantity += int(child.get("quantity", 0))
                row.order_count += 1
                row.last_order_at = datetime.utcnow()
            break
    return {"execution_id": execution_id, "order_count": row.order_count}


def record_fill(order_id, filled_quantity, status):
    if _use_db():
        try:
            return _pov_db.record_fill(
                str(order_id), int(filled_quantity), str(status)
            )
        except Exception as exc:
            logger.error("blue_engine.ledger.record_fill failed: %s", exc)
            _degrade_db(exc)
            return None
    for row in _IN_MEMORY_EXECUTIONS.values():
        for child in row.children:
            if str(child.get("orderid")) == str(order_id):
                old = int(child.get("filled_quantity", 0))
                new = max(old, min(int(filled_quantity), int(child["quantity"])))
                child["filled_quantity"] = new
                child["status"] = status
                delta = new - old
                if delta > 0:
                    if _phase_label(child) == "exiting":
                        row.exit_filled_quantity += delta
                    else:
                        row.filled_quantity += delta


def recover_filled_quantity(execution_id):
    if _use_db():
        try:
            row = _pov_db.get_execution(execution_id)
            if row is None:
                return 0
            return int(getattr(row, "filled_quantity", 0) or 0)
        except Exception as exc:
            logger.error("blue_engine.ledger.recover_filled_quantity failed: %s", exc)
            _degrade_db(exc)
            return 0
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return 0
    return int(row.filled_quantity or 0)


def get_execution(execution_id):
    if _use_db():
        try:
            return _pov_db.get_execution(execution_id)
        except Exception as exc:
            logger.error("blue_engine.ledger.get_execution failed: %s", exc)
            _degrade_db(exc)
            return None
    try:
        return _in_memory_get(execution_id)
    except KeyError:
        return None


def update_execution(execution_id, **fields):
    if _use_db():
        try:
            return _pov_db.update_execution(execution_id, **fields)
        except Exception as exc:
            logger.error("blue_engine.ledger.update_execution failed: %s", exc)
            _degrade_db(exc)
            return None
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return None
    for key, value in fields.items():
        if hasattr(row, key):
            setattr(row, key, value)
    return {"execution_id": execution_id}


# =============================================================================
# Feature 13 - VWAP, Slippage and Arrival-Price helpers
# =============================================================================
# These functions are additive: existing callers continue to work unchanged.
# The in-memory path is the authoritative implementation; the DB helpers in
# database.pov_db are optional mirrors. When the DB is unavailable we log
# and fall back to the in-memory state.
# =============================================================================


def _ensure_arrival_window(row, max_ticks: int) -> Any:
    """Return (creating if needed) the arrival-price window for this row."""
    if row.arrival_window is None:
        from blue_engine.core.benchmark import ArrivalPriceWindow
        if max_ticks < 1:
            max_ticks = _DEFAULT_ARRIVAL_TICKS
        row.arrival_window = ArrivalPriceWindow(max_ticks=max_ticks)
    return row.arrival_window


def update_vwap(execution_id, *, price, volume):
    """Fold one trade into the session VWAP and return the new value.

    ``volume`` may be 0 (LTP-only update) in which case the function
    is a no-op. Returns the new VWAP (or None when the row is missing).
    """
    if _use_db():
        try:
            vwap = _pov_db.update_vwap(
                execution_id, float(price), float(volume)
            )
            if vwap is not None:
                return float(vwap)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("blue_engine.ledger.update_vwap db path failed: %s", exc)
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return None
    if price is None or price <= 0 or volume is None or volume <= 0:
        return None if row._vwap_den == 0 else row._vwap_num / row._vwap_den
    row._vwap_num += float(price) * float(volume)
    row._vwap_den += float(volume)
    if row._vwap_den <= 0:
        return None
    return row._vwap_num / row._vwap_den


def get_vwap(execution_id):
    """Return the current session VWAP, or ``None`` if no data yet."""
    if _use_db():
        try:
            vwap = _pov_db.get_vwap(execution_id)
            if vwap is not None:
                return float(vwap)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("blue_engine.ledger.get_vwap db path failed: %s", exc)
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return None
    if row._vwap_den <= 0:
        return None
    return row._vwap_num / row._vwap_den


def open_arrival_window(execution_id, *, max_ticks: int = 20):
    """Begin (or reset) the arrival-price window for the next child fill.

    Idempotent. Use this just BEFORE ``client.placeorder(...)``.
    """
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return
    _ensure_arrival_window(row, max_ticks)
    row.arrival_window.clear()


def record_arrival_tick(execution_id, *, price, volume_weight: float = 1.0):
    """Record one mid-quote tick into the open arrival-price window."""
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return
    if row.arrival_window is None:
        open_arrival_window(execution_id)
    row.arrival_window.record(price=price, volume_weight=volume_weight)


def get_arrival_vwap(execution_id):
    """Return the current arrival-price VWAP, or ``None`` when empty."""
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return None
    if row.arrival_window is None:
        return None
    return row.arrival_window.vwap()


def close_arrival_window(execution_id):
    """Drop the arrival-price ticks after a fill (or a cancel)."""
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return
    if row.arrival_window is not None:
        row.arrival_window.clear()


def record_slippage_sample(execution_id, bps):
    """Append one slippage sample (in basis points) to the rolling window."""
    if bps is None:
        return
    try:
        value = float(bps)
    except (TypeError, ValueError):
        return
    if value != value or value in (float("inf"), float("-inf")):
        return
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return
    row.slippage_samples.append(value)


def recent_slippage_bps(execution_id, *, window: int = 10):
    """Return the rolling-mean slippage in basis points.

    Returns 0.0 when the window is empty (cold-start safe). The
    ``window`` argument is currently advisory - the deque is already
    bounded at create_execution() time, so the function simply averages
    whatever is in the buffer.
    """
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return 0.0
    if not row.slippage_samples:
        return 0.0
    return sum(row.slippage_samples) / len(row.slippage_samples)


def slippage_sample_count(execution_id):
    """Number of slippage samples currently buffered (for logging)."""
    try:
        row = _in_memory_get(execution_id)
    except KeyError:
        return 0
    return len(row.slippage_samples)


def ledger_available():
    return _LEDGER_AVAILABLE


__all__ = [
    "ledger_available",
    "make_execution_id",
    "api_key_hash",
    "create_execution",
    "claim_child",
    "record_child",
    "finish_child",
    "record_fill",
    "recover_filled_quantity",
    "get_execution",
    "update_execution",
    # ---- Feature 13 additions ----
    "update_vwap",
    "get_vwap",
    "open_arrival_window",
    "record_arrival_tick",
    "get_arrival_vwap",
    "close_arrival_window",
    "record_slippage_sample",
    "recent_slippage_bps",
    "slippage_sample_count",
]
